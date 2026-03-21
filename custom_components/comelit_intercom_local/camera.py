"""Camera entities for RTSP streams and intercom video."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging

from aiohttp import web

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .camera_utils import get_rtsp_url
from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import ComelitLocalConfigEntry, ComelitLocalCoordinator
from .models import Camera as CameraModel, PushEvent
from .placeholder import PLACEHOLDER_JPEG

_LOGGER = logging.getLogger(__name__)

_MJPEG_BOUNDARY = "frame"


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
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_camera_{camera.id}"
        self._attr_name = camera.name

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info linking this camera to its own device."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._entry_id}_camera_{self._camera.id}")},
            manufacturer=MANUFACTURER,
            name=self._camera.name,
            via_device=(DOMAIN, self._entry_id),
        )

    async def stream_source(self) -> str | None:
        """Return the RTSP stream URL for HA's stream integration."""
        url = get_rtsp_url(self._camera, self._coordinator.host)
        return url or None


class ComelitIntercomCamera(Camera):
    """Camera entity for live intercom video via ICONA Bridge UDP."""

    _attr_has_entity_name = True
    _attr_name = "Intercom Video"
    _attr_icon = "mdi:doorbell-video"
    # No ON_OFF feature — base Camera.is_on defaults to True, which is what
    # we want. Manual start/stop is handled by the separate button entities.

    def __init__(
        self,
        coordinator: ComelitLocalCoordinator,
        entry_id: str,
    ) -> None:
        """Initialize the intercom camera entity."""
        super().__init__()
        self._coordinator = coordinator
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_intercom_camera"
        self._remove_push_cb: Callable[[], None] | None = None
        self._viewer_count: int = 0

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info linking this camera to the main intercom device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name="Comelit Intercom",
        )

    @property
    def _video_active(self) -> bool:
        """Return True if a video session is currently active."""
        session = self._coordinator.video_session
        return session is not None and session.active

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
        """Return the latest JPEG frame, or placeholder when video is off."""
        session = self._coordinator.video_session
        if not session or not session.active or not session.rtp_receiver:
            return PLACEHOLDER_JPEG
        return await session.rtp_receiver.get_jpeg_frame(timeout=2.0)

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse:
        """Handle MJPEG stream — auto-starts video on open, stops on close.

        When the video session stops unexpectedly (e.g. device sends CALL_END
        and re-establishment fails), the session is automatically restarted so
        the stream stays alive as long as the client is connected.
        """
        response = web.StreamResponse()
        response.content_type = (
            f"multipart/x-mixed-replace;boundary={_MJPEG_BOUNDARY}"
        )
        await response.prepare(request)

        self._viewer_count += 1
        try:
            await self._write_mjpeg_frame(response, PLACEHOLDER_JPEG)

            while self._viewer_count > 0:
                # No active session — start (or restart) one
                if not self._video_active:
                    await self._start_video(auto_timeout=False)
                    if not self._video_active:
                        await self._write_mjpeg_frame(response, PLACEHOLDER_JPEG)
                        await asyncio.sleep(5.0)
                        continue

                # Deliver one frame (blocks up to 0.5s)
                session = self._coordinator.video_session
                if session and session.rtp_receiver:
                    frame = await session.rtp_receiver.get_jpeg_frame(
                        timeout=0.5
                    )
                    if frame:
                        _LOGGER.debug(
                            "MJPEG: delivering frame (%d bytes)", len(frame)
                        )
                    await self._write_mjpeg_frame(
                        response, frame or PLACEHOLDER_JPEG
                    )
                else:
                    await asyncio.sleep(0.5)

                # Session just died — log it; next iteration restarts
                if not self._video_active:
                    _LOGGER.info("MJPEG: video session stopped, will restart")

        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._viewer_count -= 1
            if self._viewer_count <= 0:
                self._viewer_count = 0
                await self._coordinator.async_stop_video()
        return response

    @staticmethod
    async def _write_mjpeg_frame(
        response: web.StreamResponse, jpeg_data: bytes
    ) -> None:
        """Write a single JPEG frame to the MJPEG stream."""
        header = (
            f"--{_MJPEG_BOUNDARY}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg_data)}\r\n\r\n"
        )
        await response.write(header.encode() + jpeg_data + b"\r\n")

    def _on_push(self, event: PushEvent) -> None:
        """Auto-start video on doorbell ring event."""
        if event.event_type == "doorbell_ring":
            session = self._coordinator.video_session
            if session and session.active:
                _LOGGER.debug("Video already active, skipping doorbell auto-start")
                return
            _LOGGER.info("Doorbell ring detected — starting intercom video")
            self.hass.async_create_task(self._start_video())

    async def _start_video(self, auto_timeout: bool = True) -> None:
        """Start a video call session."""
        config = self._coordinator.device_config
        if not config:
            _LOGGER.warning("Cannot start video: device config not available")
            return
        try:
            await self._coordinator.async_start_video(
                auto_timeout=auto_timeout
            )
        except Exception:
            _LOGGER.exception("Failed to start intercom video")
