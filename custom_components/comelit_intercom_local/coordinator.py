"""DataUpdateCoordinator for the Comelit Local integration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from datetime import timedelta
import logging
import time
from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .auth import authenticate
from .channels import ChannelType
from .client import IconaBridgeClient
from .config_reader import get_device_config
from .const import CONF_ENABLE_NOTIFICATIONS, DOMAIN
from .ctpp import ctpp_init_sequence
from .door import open_door
from .models import DeviceConfig, Door, PushEvent
from .push import register_push, send_push_keepalive
from .rtsp_server import LocalRtspServer
from .video_call import VideoCallSession
from .vip_listener import VipEventListener

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=30)


class ComelitLocalCoordinator(DataUpdateCoordinator[DeviceConfig]):
    """Coordinator that manages the persistent connection and push notifications."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ComelitLocalConfigEntry,
        host: str,
        port: int,
        token: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.host = host
        self.port = port
        self.token = token
        self.device_name = entry.title
        self._client: IconaBridgeClient | None = None
        self._config: DeviceConfig | None = None
        self._video_session: VideoCallSession | None = None
        self._video_stopped_by_user: bool = False
        # Prevents concurrent async_start_video calls from racing each other.
        # The device can only handle one CTPP negotiation at a time; a second
        # concurrent call would conflict and fail ~35s later with a UDPM timeout.
        self._video_start_lock: asyncio.Lock = asyncio.Lock()
        # Fires when a video session becomes ready — allows stream_source()
        # to wait briefly instead of returning None while CTPP is in flight.
        self._video_ready_event: asyncio.Event = asyncio.Event()
        self._rtsp_server: LocalRtspServer | None = None
        self._rtsp_url: str | None = None
        self._vip_listener: VipEventListener | None = None
        # LE32 counter sent in the last CTPP init on the shared connection;
        # VIP listener needs it to derive outgoing ACK timestamps
        # (ack_ts = init_ts + 0x01010000, PCAP-verified).
        self._ctpp_init_ts: int = 0
        self._keepalive_task: asyncio.Task | None = None
        # Use an insertion-ordered dict to track callbacks (value is always None).
        # This avoids ValueError on removal and preserves iteration order.
        self._push_callbacks: dict[Callable[[PushEvent], None], None] = {}
        # Async callbacks invoked at the top of async_stop_video, before any
        # RTSP client disconnect.  Lets the camera entity tear down HA's
        # Stream worker gracefully so its container_packets iterator ends
        # cleanly instead of raising "Stream ended; no additional packets"
        # on an EOF from our forced socket close.
        self._on_stop_video: dict[Callable[[], Awaitable[None]], None] = {}
        # Async callbacks invoked after video session becomes ready or is
        # fully torn down.  The camera entity uses this to write a fresh
        # HA state (is_streaming True/False) so the frontend card reacts
        # — without it, picture-entity locks to the transport it picked
        # at first stream_source() call and never upgrades from MJPEG.
        self._on_video_state_change: dict[Callable[[], Awaitable[None]], None] = {}

    @property
    def device_config(self) -> DeviceConfig | None:
        """Return the current device configuration."""
        return self._config

    @property
    def rtsp_url(self) -> str | None:
        """Return the persistent RTSP URL (available after setup)."""
        return self._rtsp_url

    @property
    def rtsp_server(self) -> LocalRtspServer | None:
        """Return the persistent RTSP server instance."""
        return self._rtsp_server

    async def _open_ctpp_channels(
        self, client: IconaBridgeClient, config: DeviceConfig
    ) -> int:
        """Open CTPP + CSPB channels and run the full init handshake.

        Called at setup and reconnect when notifications are enabled. When
        notifications are disabled, CTPP is opened lazily by door/video.

        Returns the init_ts used in the handshake so the VIP listener can
        derive its outgoing ACK timestamps from the same value.
        """
        our_addr = f"{config.apt_address}{config.apt_subaddress}"
        ctpp = await client.open_channel(
            "CTPP", ChannelType.UAUT, extra_data=our_addr
        )
        await client.open_channel("CSPB", ChannelType.UAUT)
        ts = int(time.time()) & 0xFFFFFFFF
        await ctpp_init_sequence(
            client, ctpp,
            config.apt_address, config.apt_subaddress, our_addr,
            ts,
        )
        self._ctpp_init_ts = ts
        _LOGGER.info(
            "CTPP channels opened for VIP events (address=%s, ts=0x%08X)",
            our_addr, ts,
        )
        return ts

    async def async_setup(self) -> None:
        """Connect, authenticate, fetch config, and register for push."""
        client = IconaBridgeClient(self.host, self.port)
        await client.connect()
        try:
            await authenticate(client, self.token)
            self._config = await get_device_config(client)
            await register_push(client, self._config, self._on_push_event)
        except Exception:
            await client.disconnect()
            raise

        self._client = client
        client.set_disconnect_callback(self._on_client_disconnect)

        # Start VIP event listener for doorbell ring detection, unless disabled.
        # The PUSH channel is one-shot FCM registration; actual call events
        # arrive as binary VIP messages on the CTPP channel.
        if self.config_entry.options.get(CONF_ENABLE_NOTIFICATIONS, True):
            try:
                init_ts = await self._open_ctpp_channels(client, self._config)
                vip = VipEventListener(
                    client, self._config, self._on_push_event, init_ts=init_ts,
                )
                await vip.start()
                self._vip_listener = vip
            except Exception:
                _LOGGER.warning("Failed to start VIP event listener", exc_info=True)
        else:
            _LOGGER.info("VIP event listener disabled via options")

        self._start_keepalive()

        # Start persistent RTSP server so go2rtc can connect immediately
        if not self._rtsp_server:
            rtsp = LocalRtspServer()
            self._rtsp_url = await rtsp.start()
            self._rtsp_server = rtsp
            _LOGGER.info("Persistent RTSP server started: %s", self._rtsp_url)

        self.async_set_updated_data(self._config)
        _LOGGER.info(
            "Comelit setup complete: %d doors, %d cameras",
            len(self._config.doors),
            len(self._config.cameras),
        )

    async def _reconnect(self) -> None:
        """Tear down old connection and re-establish everything."""
        self._cancel_keepalive()
        # Stop any active video session before disconnecting — a concurrent
        # session.start() holds a reference to the old client and will hang
        # for READ_TIMEOUT (30s) waiting for channel opens that will never
        # arrive once the TCP socket is closed.
        if self._video_session:
            with contextlib.suppress(Exception):
                await self._video_session.stop(reason="reconnect")
            self._video_session = None
            self._video_ready_event.clear()
            if self._rtsp_server:
                self._rtsp_server.mark_not_ready()
                self._rtsp_server.disconnect_clients()

        if self._vip_listener:
            with contextlib.suppress(Exception):
                await self._vip_listener.stop()
            self._vip_listener = None

        old_client = self._client
        self._client = None
        if old_client:
            try:
                await old_client.disconnect()
            except Exception:
                _LOGGER.debug("Error disconnecting old client", exc_info=True)

        client = IconaBridgeClient(self.host, self.port)
        try:
            await client.connect()
            await authenticate(client, self.token)
            self._config = await get_device_config(client)
            await register_push(client, self._config, self._on_push_event)
        except Exception:
            # Clean up the new client if setup fails partway through
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise

        self._client = client
        client.set_disconnect_callback(self._on_client_disconnect)

        if self.config_entry.options.get(CONF_ENABLE_NOTIFICATIONS, True):
            try:
                init_ts = await self._open_ctpp_channels(client, self._config)
                vip = VipEventListener(
                    client, self._config, self._on_push_event, init_ts=init_ts,
                )
                await vip.start()
                self._vip_listener = vip
            except Exception:
                _LOGGER.warning("Failed to start VIP listener on reconnect", exc_info=True)

        self._start_keepalive()
        _LOGGER.info("Comelit reconnected successfully")

    async def async_shutdown(self) -> None:
        """Disconnect from the device."""
        self._cancel_keepalive()
        await self.async_stop_video()
        if self._vip_listener:
            with contextlib.suppress(Exception):
                await self._vip_listener.stop()
            self._vip_listener = None
        if self._rtsp_server:
            with contextlib.suppress(Exception):
                await self._rtsp_server.stop()
            self._rtsp_server = None
            self._rtsp_url = None
        if self._client:
            await self._client.disconnect()
            self._client = None

    def add_push_callback(
        self, callback: Callable[[PushEvent], None]
    ) -> Callable[[], None]:
        """Register a push event callback. Returns a callable that removes it."""
        self._push_callbacks[callback] = None

        def _remove() -> None:
            self._push_callbacks.pop(callback, None)

        return _remove

    def add_stop_video_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> Callable[[], None]:
        """Register an async callback invoked when video is stopped."""
        self._on_stop_video[callback] = None

        def _remove() -> None:
            self._on_stop_video.pop(callback, None)

        return _remove

    def add_video_state_change_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> Callable[[], None]:
        """Register an async callback invoked after video becomes ready or stops."""
        self._on_video_state_change[callback] = None

        def _remove() -> None:
            self._on_video_state_change.pop(callback, None)

        return _remove

    async def _notify_video_state_change(self) -> None:
        """Fire all registered video state-change callbacks."""
        for cb in list(self._on_video_state_change):
            try:
                await cb()
            except Exception:
                _LOGGER.exception("Error in video state change callback")

    def _on_push_event(self, event: PushEvent) -> None:
        """Dispatch a push event to all registered callbacks."""
        for cb in list(self._push_callbacks):
            try:
                cb(event)
            except Exception:
                _LOGGER.exception("Error in push callback")

    async def async_open_door(self, door: Door) -> None:
        """Open a door.

        Three paths depending on what CTPP channel is currently open:

        1. Video active — send a single 0x1840/0x000D on the video CTPP channel
           (PCAP-verified Android app behaviour; no new channel or 6-step sequence).
        2. VIP listener has CTPP open (notifications ON, no video) — reuse it,
           fire OPEN_DOOR + CONFIRM directly (~30ms, no init overhead).
        3. No CTPP open (notifications OFF) — open a transient CTPP channel,
           run full init, send commands, close channel.
        """
        if not self._config or not self._client:
            raise RuntimeError("Not connected")
        if self._video_session and self._video_session.active:
            our_addr = f"{self._config.apt_address}{self._config.apt_subaddress}"
            entrance_addr = self._config.caller_address or our_addr
            await self._video_session.async_open_door_on_ctpp(
                our_addr, entrance_addr, door.output_index
            )
        else:
            await open_door(self._client, self._config, door)

    async def async_start_video(
        self, auto_timeout: bool = True, by_user: bool = False
    ) -> VideoCallSession:
        """Start a video call session.

        Concurrent calls are dropped — the device can only negotiate one
        CTPP session at a time and a second concurrent start would conflict
        with the first and fail ~35 s later with a UDPM timeout.

        Args:
            auto_timeout: stop the session after VIDEO_SESSION_TIMEOUT seconds.
            by_user: True when called from an explicit user action (button press).
                     False for auto-restarts from CALL_END / timeout callbacks.
                     Auto-restarts are silently dropped if the user has
                     since stopped video (prevents a stale async_create_task
                     from overriding a user stop and causing an infinite
                     go2rtc reconnect loop).
        """
        if not self._config:
            raise RuntimeError("Not configured")

        if self._video_start_lock.locked():
            _LOGGER.debug("Video start already in progress — skipping duplicate call")
            if self._video_session:
                return self._video_session
            raise RuntimeError("Video start already in progress")

        async with self._video_start_lock:
            if not self._client:
                raise RuntimeError("Not connected")

            # Drop auto-restarts that arrive after the user has stopped video.
            # Race: _on_video_call_end schedules async_start_video() as a task;
            # the user may stop video before the task executes.  Without this
            # check the stale task would reset _video_stopped_by_user and call
            # mark_ready(), causing go2rtc to reconnect into a dead stream.
            if self._video_stopped_by_user and not by_user:
                _LOGGER.debug("Skipping auto-restart — video was stopped by user")
                raise RuntimeError("Video was stopped by user — not auto-restarting")

            # If the TCP connection died (120s receive-loop timeout) before the
            # health-check interval had a chance to reconnect, reconnect now so
            # we don't start a session on a dead socket and wait 30s for UDPM
            # to time out.
            if not self._client.connected:
                _LOGGER.info("Client disconnected — reconnecting before video start")
                try:
                    await self._reconnect()
                except Exception as err:
                    raise RuntimeError(f"Reconnect failed: {err}") from err

            self._video_stopped_by_user = False
            await self.async_stop_video()

            # Pause the VIP listener task so it doesn't consume CTPP messages
            # meant for the video session. The CTPP channel itself stays open
            # and will be reused by the video session directly (no rename needed).
            # The listener task restarts in async_stop_video via _ensure_vip_listener.
            if self._vip_listener:
                with contextlib.suppress(Exception):
                    await self._vip_listener.stop_task()
                self._vip_listener = None

            t0 = time.monotonic()
            _LOGGER.info("Video session starting (CTPP setup)")
            session = VideoCallSession(
                self._client,
                self._config,
                auto_timeout=auto_timeout,
                rtsp_server=self._rtsp_server,
                on_call_end=self._on_video_call_end,
                on_timeout=self._on_video_call_end,
            )
            # Publish the session ONLY after start() has completed its
            # readiness gate (first real NAL queued).  Publishing earlier
            # lets HA's stream worker open the RTSP URL while CTPP is
            # still negotiating — it probes a video-less stream, stalls,
            # and takes ~20 s extra to recover once real NALs finally
            # arrive.  The trade-off is a cosmetic "camera does not
            # support play stream service" error logged by Lovelace at
            # the ~2 s mark, because `stream_source()` returns None while
            # CTPP is in flight.  go2rtc's WebRTC path queries the URL
            # through a different code path and is not affected, so the
            # user-visible latency stays at ~3 s.
            await session.start()
            _LOGGER.info("Video session ready in %.1fs", time.monotonic() - t0)
            self._video_session = session
            self._video_ready_event.set()
            # Unblock PLAY handlers that have been waiting inside the RTSP
            # server for video to actually flow.  Any stream_worker that
            # reconnected during the CTPP handshake is stalled on PLAY
            # (our server holds 200 OK until mark_ready); releasing it here
            # means it transitions straight to reading frames instead of
            # erroring on an empty stream and taking a 10 s HA backoff.
            if self._rtsp_server:
                self._rtsp_server.mark_ready()
            await self._notify_video_state_change()
            return session

    def _on_video_call_end(self) -> None:
        """Called by VideoCallSession when the device sends CALL_END."""
        if self._video_stopped_by_user:
            return
        _LOGGER.info("CALL_END received — scheduling session restart")
        self.hass.async_create_task(self._auto_restart_video())

    async def _auto_restart_video(self) -> None:
        """Auto-restart video after CALL_END or timeout.

        Calls async_start_video() without by_user=True so the call is
        silently dropped if the user has stopped video in the meantime.
        RuntimeError from that path is caught here to avoid HA logging an
        unhandled task exception for a normal, expected situation.
        """
        try:
            await self.async_start_video()
        except RuntimeError as err:
            _LOGGER.debug("Auto-restart skipped: %s", err)
        except Exception:
            _LOGGER.warning("Auto-restart failed", exc_info=True)

    @property
    def video_stopped_by_user(self) -> bool:
        """Return True if the user explicitly stopped video (not CALL_END)."""
        return self._video_stopped_by_user

    def request_video_stop(self) -> None:
        """Mark that the user explicitly requested video to stop."""
        self._video_stopped_by_user = True

    def _start_keepalive(self) -> None:
        """Start the background keepalive task (cancels any previous one)."""
        self._cancel_keepalive()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _cancel_keepalive(self) -> None:
        """Cancel the keepalive task if running."""
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

    async def _keepalive_loop(self) -> None:
        """Send a periodic push-info probe to keep the TCP connection alive.

        The Comelit device sleeps when idle and stops sending TCP traffic.
        Without this probe, the receive-loop 120s timeout fires and triggers
        a full reconnect cycle.  Re-sending push-info every 90s causes the
        device to respond with a JSON ACK, resetting the idle timer.

        If the device is genuinely unreachable (half-open socket), send_json
        raises within 10s (ProtocolError timeout) — the receive-loop will
        also detect the dead connection shortly after and trigger reconnect.
        """
        KEEPALIVE_INTERVAL = 90
        KEEPALIVE_TIMEOUT = 10.0

        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if not self._client or not self._client.connected:
                return
            try:
                await asyncio.wait_for(
                    send_push_keepalive(self._client, self._config),
                    timeout=KEEPALIVE_TIMEOUT,
                )
                _LOGGER.debug("Keepalive OK")
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.debug("Keepalive failed — connection may be dead", exc_info=True)
                # Don't force-reconnect here; the receive-loop will detect the
                # dead socket and set client.connected = False within seconds,
                # which the coordinator's health-check will pick up.

    async def _ensure_vip_listener(self) -> None:
        """Start VIP listener if enabled and not already running.

        Reuses the init_ts stored from the most recent _open_ctpp_channels
        call so the restarted listener's outgoing ACKs match the counter
        state the device already has for this CTPP channel.
        """
        if self._vip_listener or not self._config or not self._client:
            return
        if not self.config_entry.options.get(CONF_ENABLE_NOTIFICATIONS, True):
            return
        try:
            vip = VipEventListener(
                self._client, self._config, self._on_push_event,
                init_ts=self._ctpp_init_ts,
            )
            await vip.start()
            self._vip_listener = vip
            _LOGGER.info("VIP event listener restarted")
        except Exception:
            _LOGGER.warning("Failed to restart VIP listener", exc_info=True)

    async def async_stop_video(self) -> None:
        """Stop the active video call session.

        Snapshots _video_session and clears it immediately so a concurrent
        async_stop_video call can't also try to stop the same session
        (previous behaviour raced and crashed with AttributeError when the
        first stop cleared the attribute while the second was awaiting a
        stop-callback).
        """
        session = self._video_session
        if session is None:
            return
        self._video_session = None
        self._video_ready_event.clear()

        # Tear HA's Stream worker down gracefully FIRST, before any
        # forced RTSP client disconnect.  Stream.stop() joins the
        # worker thread, so its container closes cleanly; without
        # this, disconnect_clients() triggers an EOF mid-read and
        # HA logs "Stream ended; no additional packets" plus a 10 s
        # backoff before the next Start can recover.
        for cb in list(self._on_stop_video):
            try:
                await cb()
            except Exception:
                _LOGGER.exception("Error in stop-video callback")

        await session.stop(reason="user stopped")
        # Block future PLAYs until the next session is ready, and
        # kick any remaining RTSP clients (e.g. go2rtc) so they
        # reconnect fresh against a stream that already has video.
        if self._rtsp_server:
            self._rtsp_server.mark_not_ready()
            self._rtsp_server.disconnect_clients()
        # Restart VIP listener now that video released the CTPP slot.
        # Skip if we're inside async_start_video (lock already held) —
        # start_video will stop VIP again immediately anyway.
        if not self._video_start_lock.locked():
            await self._ensure_vip_listener()
        await self._notify_video_state_change()

    @property
    def video_session(self) -> VideoCallSession | None:
        """Return the active video call session, if any."""
        return self._video_session

    def _on_client_disconnect(self) -> None:
        """Called by the TCP client when the connection drops unexpectedly.

        Schedules an immediate coordinator refresh so _async_update_data runs
        within milliseconds and triggers reconnect, instead of waiting for the
        next 30-second polling interval.
        """
        if self._client is None:
            return  # already shut down
        _LOGGER.debug("TCP connection lost — scheduling immediate reconnect")
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_update_data(self) -> DeviceConfig:
        """Health-check the connection; reconnect if needed."""
        if self._client and self._client.connected:
            if self._config:
                return self._config

        # Connection lost or no config — attempt reconnect
        _LOGGER.warning("Comelit device disconnected, attempting reconnect")
        try:
            await self._reconnect()
        except Exception as err:
            raise UpdateFailed(f"Reconnect failed: {err}") from err

        return self._config  # type: ignore[return-value]


ComelitLocalConfigEntry: TypeAlias = ConfigEntry[ComelitLocalCoordinator]
