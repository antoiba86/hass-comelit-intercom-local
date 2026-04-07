"""DataUpdateCoordinator for the Comelit Local integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from datetime import timedelta
import logging
import time
from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .auth import authenticate
from .client import IconaBridgeClient
from .config_reader import get_device_config
from .const import DOMAIN
from .door import open_door
from .models import DeviceConfig, Door, PushEvent
from .push import register_push
from .rtsp_server import LocalRtspServer
from .video_call import VideoCallSession

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
        # Use an insertion-ordered dict to track callbacks (value is always None).
        # This avoids ValueError on removal and preserves iteration order.
        self._push_callbacks: dict[Callable[[PushEvent], None], None] = {}

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
        _LOGGER.info("Comelit reconnected successfully")

    async def async_shutdown(self) -> None:
        """Disconnect from the device."""
        await self.async_stop_video()
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

    def _on_push_event(self, event: PushEvent) -> None:
        """Dispatch a push event to all registered callbacks."""
        for cb in list(self._push_callbacks):
            try:
                cb(event)
            except Exception:
                _LOGGER.exception("Error in push callback")

    async def async_open_door(self, door: Door) -> None:
        """Open a door using a fresh connection."""
        if not self._config:
            raise RuntimeError("Not configured")
        await open_door(self.host, self.port, self.token, self._config, door)

    async def async_start_video(
        self, auto_timeout: bool = True
    ) -> VideoCallSession:
        """Start a video call session.

        Concurrent calls are dropped — the device can only negotiate one
        CTPP session at a time and a second concurrent start would conflict
        with the first and fail ~35 s later with a UDPM timeout.
        """
        if not self._config:
            raise RuntimeError("Not configured")

        if self._video_start_lock.locked():
            _LOGGER.debug("Video start already in progress — skipping duplicate call")
            if self._video_session:
                return self._video_session
            raise RuntimeError("Video start already in progress")

        async with self._video_start_lock:
            self._video_stopped_by_user = False
            await self.async_stop_video()

            t0 = time.monotonic()
            _LOGGER.info("Video session starting (CTPP setup)")
            session = VideoCallSession(
                self.host, self.port, self.token, self._config,
                auto_timeout=auto_timeout,
                rtsp_server=self._rtsp_server,
                on_call_end=self._on_video_call_end,
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
            return session

    def _on_video_call_end(self) -> None:
        """Called by VideoCallSession when the device sends CALL_END."""
        if self._video_stopped_by_user:
            return
        _LOGGER.info("CALL_END received — scheduling session restart")
        self.hass.async_create_task(self.async_start_video())

    @property
    def video_stopped_by_user(self) -> bool:
        """Return True if the user explicitly stopped video (not CALL_END)."""
        return self._video_stopped_by_user

    def request_video_stop(self) -> None:
        """Mark that the user explicitly requested video to stop."""
        self._video_stopped_by_user = True

    async def async_stop_video(self) -> None:
        """Stop the active video call session."""
        if self._video_session:
            await self._video_session.stop(reason="user stopped")
            self._video_session = None
            self._video_ready_event.clear()

    @property
    def video_session(self) -> VideoCallSession | None:
        """Return the active video call session, if any."""
        return self._video_session

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
