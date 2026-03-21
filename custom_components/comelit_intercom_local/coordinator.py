"""DataUpdateCoordinator for the Comelit Local integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from datetime import timedelta
import logging
from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .auth import authenticate
from .client import IconaBridgeClient
from .config_reader import get_device_config
from .const import DOMAIN, PREWARM_DELAY_SECONDS
from .door import open_door
from .models import DeviceConfig, Door, PushEvent
from .push import register_push
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
        self._prewarm_task: asyncio.Task[None] | None = None
        # Use an insertion-ordered dict to track callbacks (value is always None).
        # This avoids ValueError on removal and preserves iteration order.
        self._push_callbacks: dict[Callable[[PushEvent], None], None] = {}

    @property
    def device_config(self) -> DeviceConfig | None:
        """Return the current device configuration."""
        return self._config

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
        self._cancel_prewarm()
        await self.async_stop_video()
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
        """Start a video call session."""
        if not self._config:
            raise RuntimeError("Not configured")

        self._video_stopped_by_user = False

        await self.async_stop_video()  # Also cancels prewarm

        session = VideoCallSession(
            self.host, self.port, self.token, self._config,
            auto_timeout=auto_timeout,
        )
        await session.start()
        self._video_session = session

        # For long-running MJPEG streams (auto_timeout=False), schedule a
        # pre-warm at t=25s so the next session is ready before CALL_END arrives
        # (~30s). Establishment takes ~1.3s, leaving ~3.7s buffer.
        if not auto_timeout:
            self._prewarm_task = asyncio.create_task(self._prewarm_loop())

        return session

    def _cancel_prewarm(self) -> None:
        """Cancel the pre-warm task without awaiting it.

        Not awaiting is intentional: awaiting a mid-establishment task blocks
        for 14-18s while the TCP handshake times out. We fire cancel() and let
        the task handle its own cleanup asynchronously.
        """
        task, self._prewarm_task = self._prewarm_task, None
        if task and not task.done():
            task.cancel()

    def _stop_session_in_background(
        self, session: VideoCallSession, label: str
    ) -> None:
        """Schedule a session stop in the background without blocking."""
        async def _stop() -> None:
            try:
                await session.stop()
            except Exception:
                _LOGGER.debug("Error stopping %s session", label, exc_info=True)

        asyncio.get_running_loop().create_task(_stop())

    async def _prewarm_loop(self) -> None:
        """Establish the next session at t=25s, then swap it in atomically."""
        try:
            await asyncio.sleep(PREWARM_DELAY_SECONDS)
        except asyncio.CancelledError:
            return  # Cancelled during sleep — nothing to clean up.

        if self._video_stopped_by_user or not self._config:
            return

        _LOGGER.info("Pre-warming next video session")
        new_session = VideoCallSession(
            self.host, self.port, self.token, self._config,
            auto_timeout=False,
        )
        try:
            await new_session.start()
        except asyncio.CancelledError:
            self._stop_session_in_background(new_session, "cancelled pre-warm")
            return
        except Exception:
            _LOGGER.warning(
                "Pre-warm failed — will restart on CALL_END instead",
                exc_info=True,
            )
            return

        if self._video_stopped_by_user:
            # User stopped video while we were establishing — discard new session.
            await new_session.stop()
            return

        # Wait for the new receiver to produce at least one frame before
        # swapping, so camera.py never shows a placeholder during the switch.
        new_receiver = new_session.rtp_receiver
        if new_receiver:
            first_frame = await new_receiver.get_jpeg_frame(timeout=3.0)
            if not first_frame:
                _LOGGER.warning("Pre-warm: new session produced no frames in 3s")

        if self._video_stopped_by_user:
            await new_session.stop()
            return

        # Atomic swap: camera.py will pick up the new session on its next frame.
        old_session, self._video_session = self._video_session, new_session
        _LOGGER.info("Switched to pre-warmed session seamlessly")

        if old_session:
            self._stop_session_in_background(old_session, "old")

        # Schedule the next pre-warm for the new session (infinite cycling).
        self._prewarm_task = asyncio.create_task(self._prewarm_loop())

    @property
    def video_stopped_by_user(self) -> bool:
        """Return True if the user explicitly stopped video (not CALL_END)."""
        return self._video_stopped_by_user

    def request_video_stop(self) -> None:
        """Mark that the user explicitly requested video to stop."""
        self._video_stopped_by_user = True

    async def async_stop_video(self) -> None:
        """Stop the active video call session."""
        self._cancel_prewarm()
        if self._video_session:
            await self._video_session.stop()
            self._video_session = None

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
