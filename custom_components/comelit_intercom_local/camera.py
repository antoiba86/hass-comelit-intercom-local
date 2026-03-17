"""Camera entities for RTSP streams and intercom video."""

from __future__ import annotations

from collections.abc import Callable
import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .camera_utils import get_rtsp_url
from .coordinator import ComelitLocalConfigEntry, ComelitLocalCoordinator
from .models import Camera as CameraModel, PushEvent

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ComelitLocalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera entities from device config."""
    coordinator = entry.runtime_data
    config = coordinator.device_config
    if not config:
        return

    entities: list[Camera] = [
        ComelitCamera(coordinator, cam, entry.entry_id)
        for cam in config.cameras
        if cam.rtsp_url
    ]

    # Add intercom camera if there are doors (i.e. the device has an intercom)
    if config.doors:
        entities.append(ComelitIntercomCamera(coordinator, entry.entry_id))

    async_add_entities(entities)


class ComelitCamera(Camera):
    """Camera entity that provides an RTSP stream."""

    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: ComelitLocalCoordinator,
        camera: CameraModel,
        entry_id: str,
    ) -> None:
        """Initialize the camera entity."""
        super().__init__()
        self._coordinator = coordinator
        self._camera = camera
        self._attr_unique_id = f"{entry_id}_camera_{camera.id}"
        self._attr_name = camera.name

    async def stream_source(self) -> str | None:
        """Return the RTSP stream URL for HA's stream integration."""
        url = get_rtsp_url(self._camera, self._coordinator.host)
        return url or None


class ComelitIntercomCamera(Camera):
    """Camera entity for live intercom video via ICONA Bridge UDP."""

    _attr_has_entity_name = True
    _attr_name = "Intercom Video"
    _attr_icon = "mdi:doorbell-video"
    _attr_supported_features = CameraEntityFeature.ON_OFF

    def __init__(
        self,
        coordinator: ComelitLocalCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the intercom camera entity."""
        super().__init__()
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry_id}_intercom_camera"
        self._remove_push_cb: Callable[[], None] | None = None

    @property
    def is_on(self) -> bool:
        """Return True if video session is active."""
        session = self._coordinator.video_session
        return session is not None and session.active

    async def async_turn_on(self) -> None:
        """Start the video call."""
        await self._start_video()

    async def async_turn_off(self) -> None:
        """Stop the video call."""
        await self._coordinator.async_stop_video()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register for push events when entity is added."""
        self._remove_push_cb = self._coordinator.add_push_callback(
            self._on_push
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister push callback when entity is removed."""
        if self._remove_push_cb:
            self._remove_push_cb()
            self._remove_push_cb = None
        await self._coordinator.async_stop_video()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest JPEG frame from the active video session."""
        session = self._coordinator.video_session
        if not session or not session.active or not session.rtp_receiver:
            return None
        return await session.rtp_receiver.get_jpeg_frame(timeout=2.0)

    def _on_push(self, event: PushEvent) -> None:
        """Auto-start video on doorbell ring event."""
        if event.event_type == "doorbell_ring":
            _LOGGER.info("Doorbell ring detected — starting intercom video")
            self.hass.async_create_task(self._start_video())

    async def _start_video(self) -> None:
        """Start a video call session."""
        config = self._coordinator.device_config
        if not config:
            _LOGGER.warning("Cannot start video: device config not available")
            return
        try:
            await self._coordinator.async_start_video()
            self.async_write_ha_state()
        except Exception:
            _LOGGER.exception("Failed to start intercom video")
