"""Camera entities for RTSP streams and intercom video."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

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
    """Camera entity for live intercom video and audio via go2rtc/WebRTC.

    Serves a persistent local RTSP stream (started at integration load) so
    go2rtc can connect immediately. When a video call session is active,
    H.264 video and G.711 audio flow through to the browser via WebRTC.

    `CameraEntityFeature.STREAM` is required for go2rtc's WebRTC path to
    work — without it, HA falls back to MJPEG polling via
    `async_camera_image()` at ~2 fps, wasting the 25 fps we get from the
    device.  The 24 s first-call HLS warmup that the stream worker would
    otherwise cause is avoided by gating `stream_source()` — it returns
    None until a video session is active, so the stream worker cannot
    connect at HA boot and freeze its codec context with pix_fmt=-1.
    """

    _attr_has_entity_name = True
    _attr_name = "Live Feed"
    _attr_icon = "mdi:doorbell-video"
    _attr_supported_features = CameraEntityFeature.STREAM

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
        self._remove_stop_video_cb: Callable[[], None] | None = None
        self._remove_state_cb: Callable[[], None] | None = None

    @property
    def is_streaming(self) -> bool:
        """Reflect whether a video session is currently active.

        HA maps this to the entity state ("streaming" vs "idle"), which the
        Lovelace card uses to decide between the live picture-entity and
        the idle thumbnail.  Without a truthful is_streaming, picture-entity
        locks onto the transport it picked at first stream_source() call —
        MJPEG if the session wasn't ready yet — and never upgrades.
        """
        return self._coordinator.video_session is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info linking this camera to the main intercom device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=self._coordinator.device_name,
        )

    async def stream_source(self) -> str | None:
        """Return the RTSP URL, waiting briefly if a session is starting.

        Gating on `_video_ready_event` prevents HA's stream worker and
        go2rtc from connecting during the 2–3 s CTPP handshake window —
        if they connect before video RTP flows, ffmpeg's demuxer errors
        with "Stream ended; no additional packets" and HA backs off ~10 s
        before retrying, which is the dominant delay in time-to-first-frame.

        If a session is already active, return the URL immediately.
        If one is in flight, wait up to 5 s for it.  Return None if
        nothing is starting — HA falls back to JPEG polling in that case.
        """
        if self._coordinator.video_session is not None:
            return self._coordinator.rtsp_url
        try:
            await asyncio.wait_for(
                self._coordinator._video_ready_event.wait(), timeout=5.0
            )
            return self._coordinator.rtsp_url
        except TimeoutError:
            return None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the latest JPEG frame, or placeholder when video is off."""
        session = self._coordinator.video_session
        if not session or not session.active or not session.rtp_receiver:
            return PLACEHOLDER_JPEG
        return await session.rtp_receiver.get_jpeg_frame(timeout=2.0)

    async def async_added_to_hass(self) -> None:
        """Register for push events when entity is added."""
        self._remove_push_cb = self._coordinator.add_push_callback(self._on_push)
        self._remove_stop_video_cb = self._coordinator.add_stop_video_callback(
            self._async_stop_ha_stream
        )
        self._remove_state_cb = self._coordinator.add_video_state_change_callback(
            self._async_video_state_changed
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister callbacks when entity is removed."""
        if self._remove_push_cb:
            self._remove_push_cb()
            self._remove_push_cb = None
        if self._remove_stop_video_cb:
            self._remove_stop_video_cb()
            self._remove_stop_video_cb = None
        if self._remove_state_cb:
            self._remove_state_cb()
            self._remove_state_cb = None
        await self._coordinator.async_stop_video()

    async def _async_video_state_changed(self) -> None:
        """Push a fresh state to HA when video session starts or stops.

        Also re-runs provider discovery.  HA caches the WebRTC provider lookup
        at entity-add time by calling `stream_source()` once — when the
        session isn't active yet it returns None, so go2rtc is never attached
        and `frontend_stream_types` only advertises HLS.  Refreshing here
        lets go2rtc claim the camera on the first video session, which
        upgrades the picture-entity card from HLS to WebRTC.
        """
        await self.async_refresh_providers()
        self.async_write_ha_state()

    async def _async_stop_ha_stream(self) -> None:
        """Tear down HA's cached Stream so the worker thread exits cleanly.

        Called from the coordinator before it forces any RTSP client
        disconnect.  Clearing `self.stream` ensures HA re-invokes
        `stream_source()` on the next Start, which properly waits on
        `_video_ready_event`.
        """
        stream = self.stream
        if stream is None:
            return
        self.stream = None
        try:
            await stream.stop()
        except Exception:
            _LOGGER.debug("Error stopping HA stream", exc_info=True)

    def _on_push(self, event: PushEvent) -> None:
        """Handle push events — no auto-start; user controls video via button or automation."""
