"""DataUpdateCoordinator for the Comelit Local integration."""

from __future__ import annotations

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
from .const import DOMAIN
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

    async def async_start_video(self) -> VideoCallSession:
        """Start a video call session using a fresh connection."""
        if not self._config:
            raise RuntimeError("Not configured")
        # Stop any existing session first
        await self.async_stop_video()
        session = VideoCallSession(
            self.host, self.port, self.token, self._config
        )
        await session.start()
        self._video_session = session
        return session

    async def async_stop_video(self) -> None:
        """Stop the active video call session."""
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
