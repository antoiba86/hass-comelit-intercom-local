"""Tests for camera entities — placeholder image and stream_source gating."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.camera import (
    ComelitIntercomCamera,
)
from custom_components.comelit_intercom_local.placeholder import PLACEHOLDER_JPEG
from custom_components.comelit_intercom_local.models import PushEvent


# ---------------------------------------------------------------------------
# Placeholder JPEG validity
# ---------------------------------------------------------------------------

def test_placeholder_jpeg_valid():
    """Placeholder JPEG starts with SOI and ends with EOI markers."""
    assert PLACEHOLDER_JPEG[:2] == b"\xff\xd8"  # SOI
    assert PLACEHOLDER_JPEG[-2:] == b"\xff\xd9"  # EOI
    assert len(PLACEHOLDER_JPEG) > 100


# ---------------------------------------------------------------------------
# Camera fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def camera() -> ComelitIntercomCamera:
    """Create a ComelitIntercomCamera with a mocked coordinator."""
    coordinator = MagicMock()
    coordinator.video_session = None
    coordinator.device_config = MagicMock()
    coordinator._video_ready_event = asyncio.Event()
    cam = ComelitIntercomCamera(coordinator, "test_entry")
    return cam


# ---------------------------------------------------------------------------
# async_camera_image
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_camera_image_returns_placeholder_when_no_session(camera):
    """async_camera_image returns placeholder JPEG when no video session."""
    result = await camera.async_camera_image()
    assert result is PLACEHOLDER_JPEG


@pytest.mark.asyncio
async def test_camera_image_returns_placeholder_when_session_inactive(camera):
    """async_camera_image returns placeholder when session exists but inactive."""
    session = MagicMock()
    session.active = False
    camera._coordinator.video_session = session

    result = await camera.async_camera_image()
    assert result is PLACEHOLDER_JPEG


@pytest.mark.asyncio
async def test_camera_image_returns_frame_when_active(camera):
    """async_camera_image returns RTP frame when session is active."""
    fake_frame = b"\xff\xd8fake_jpeg\xff\xd9"
    session = MagicMock()
    session.active = True
    session.rtp_receiver = MagicMock()
    session.rtp_receiver.get_jpeg_frame = AsyncMock(return_value=fake_frame)
    camera._coordinator.video_session = session

    result = await camera.async_camera_image()
    assert result == fake_frame


# ---------------------------------------------------------------------------
# stream_source
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_source_returns_url_when_session_active(camera):
    """stream_source returns RTSP URL immediately when a session exists."""
    camera._coordinator.video_session = MagicMock()
    camera._coordinator.rtsp_url = "rtsp://127.0.0.1:12345/intercom"

    url = await camera.stream_source()
    assert url == "rtsp://127.0.0.1:12345/intercom"


@pytest.mark.asyncio
async def test_stream_source_returns_none_when_no_session_and_timeout(camera):
    """stream_source returns None when no session starts within the timeout."""
    camera._coordinator.video_session = None
    # Patch wait_for to immediately raise TimeoutError so the test doesn't
    # actually sleep for the full 5s internal timeout.
    with patch(
        "custom_components.comelit_intercom_local.camera.asyncio.wait_for",
        side_effect=TimeoutError,
    ):
        url = await camera.stream_source()
    assert url is None


@pytest.mark.asyncio
async def test_stream_source_returns_url_when_event_fires(camera):
    """stream_source returns RTSP URL once _video_ready_event is set."""
    camera._coordinator.video_session = None
    camera._coordinator.rtsp_url = "rtsp://127.0.0.1:12345/intercom"

    async def set_event_soon():
        await asyncio.sleep(0.05)
        camera._coordinator._video_ready_event.set()

    asyncio.create_task(set_event_soon())

    url = await camera.stream_source()
    assert url == "rtsp://127.0.0.1:12345/intercom"


# ---------------------------------------------------------------------------
# Doorbell push guard
# ---------------------------------------------------------------------------

def test_on_push_skips_when_already_active(camera):
    """_on_push does not start video if session already active."""
    session = MagicMock()
    session.active = True
    camera._coordinator.video_session = session
    camera.hass = MagicMock()

    event = PushEvent(event_type="doorbell_ring")
    camera._on_push(event)

    camera.hass.async_create_task.assert_not_called()


def test_on_push_starts_video_when_inactive(camera):
    """_on_push starts video if no active session."""
    camera._coordinator.video_session = None
    camera.hass = MagicMock()

    event = PushEvent(event_type="doorbell_ring")
    camera._on_push(event)

    camera.hass.async_create_task.assert_called_once()


# ---------------------------------------------------------------------------
# _start_video
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_video_calls_coordinator(camera):
    """_start_video calls coordinator.async_start_video with auto_timeout=False."""
    camera._coordinator.async_start_video = AsyncMock()

    await camera._start_video()

    camera._coordinator.async_start_video.assert_called_once_with(auto_timeout=False)


@pytest.mark.asyncio
async def test_start_video_skips_when_no_config(camera):
    """_start_video returns early if device_config is not available."""
    camera._coordinator.device_config = None
    camera._coordinator.async_start_video = AsyncMock()

    await camera._start_video()

    camera._coordinator.async_start_video.assert_not_called()
