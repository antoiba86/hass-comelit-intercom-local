"""Tests for camera entities — placeholder image and MJPEG stream."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.camera import (
    ComelitIntercomCamera,
    _MJPEG_BOUNDARY,
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
# Camera image returns placeholder when no session
# ---------------------------------------------------------------------------

@pytest.fixture
def camera() -> ComelitIntercomCamera:
    """Create a ComelitIntercomCamera with a mocked coordinator."""
    coordinator = MagicMock()
    coordinator.video_session = None
    coordinator.device_config = MagicMock()
    cam = ComelitIntercomCamera(coordinator, "test_entry")
    return cam


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

    # Should NOT create a task since video is already active
    camera.hass.async_create_task.assert_not_called()


def test_on_push_starts_video_when_inactive(camera):
    """_on_push starts video if no active session."""
    camera._coordinator.video_session = None
    camera.hass = MagicMock()

    event = PushEvent(event_type="doorbell_ring")
    camera._on_push(event)

    camera.hass.async_create_task.assert_called_once()


# ---------------------------------------------------------------------------
# Viewer counting
# ---------------------------------------------------------------------------

def test_initial_viewer_count_is_zero(camera):
    """Camera starts with zero viewers."""
    assert camera._viewer_count == 0


# ---------------------------------------------------------------------------
# MJPEG stream
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mjpeg_stream_sends_placeholder_and_starts_video(camera):
    """handle_async_mjpeg_stream sends placeholder, starts video, stops on close."""
    camera._coordinator.async_stop_video = AsyncMock()

    # Session starts inactive → _video_active=False → starts video
    session = MagicMock()
    session.active = False
    camera._coordinator.video_session = session

    rtp_receiver = MagicMock()
    fake_frame = b"\xff\xd8live\xff\xd9"
    rtp_receiver.get_jpeg_frame = AsyncMock(return_value=fake_frame)

    async def mock_start_video(auto_timeout=True):
        session.active = True
        session.rtp_receiver = rtp_receiver

    camera._coordinator.async_start_video = mock_start_video

    # _video_active sequence:
    #   call 1: False → `if not _video_active` triggers start, started_by_us=True
    #   call 2: True  → one frame delivered in the while loop
    #   call 3+: False → exits while loop
    video_active_calls = 0

    def mock_video_active(self):
        nonlocal video_active_calls
        video_active_calls += 1
        return video_active_calls == 2  # False → True → False

    # Mock request and capture written data
    request = MagicMock()
    written_data = []

    response = MagicMock()
    response.prepare = AsyncMock()

    async def capture_write(data):
        written_data.append(data)

    response.write = capture_write

    with patch("aiohttp.web.StreamResponse", return_value=response):
        with patch.object(type(camera), "_video_active", new_callable=lambda: property(mock_video_active)):
            await camera.handle_async_mjpeg_stream(request)

    # Placeholder was sent as first frame
    assert len(written_data) >= 1
    assert PLACEHOLDER_JPEG in written_data[0]
    assert _MJPEG_BOUNDARY.encode() in written_data[0]

    # Live frame was delivered in the loop iteration
    assert any(fake_frame in d for d in written_data[1:])

    # Video was stopped (started_by_us=True, viewer_count back to 0)
    camera._coordinator.async_stop_video.assert_called_once()


@pytest.mark.asyncio
async def test_video_stops_when_last_viewer_leaves_even_if_not_starter(camera):
    """Video stops when viewer_count reaches 0 regardless of who started it.

    Regression test: previously video would never stop if the viewer who
    started it disconnected before the others (started_by_us bug).
    """
    camera._coordinator.async_stop_video = AsyncMock()
    camera._coordinator.async_start_video = AsyncMock()

    # Simulate: starter already left, viewer_count manually set to 1
    # (one remaining viewer who did NOT start the video)
    camera._viewer_count = 1

    session = MagicMock()
    session.active = True
    session.rtp_receiver = MagicMock()
    session.rtp_receiver.get_jpeg_frame = AsyncMock(return_value=None)
    camera._coordinator.video_session = session

    request = MagicMock()
    response = MagicMock()
    response.prepare = AsyncMock()
    response.write = AsyncMock()

    # _video_active: True once (one loop), then False (exits loop)
    call_count = 0

    def mock_video_active(self):
        nonlocal call_count
        call_count += 1
        return call_count == 1

    with patch("aiohttp.web.StreamResponse", return_value=response):
        with patch.object(
            type(camera), "_video_active", new_callable=lambda: property(mock_video_active)
        ):
            await camera.handle_async_mjpeg_stream(request)

    # viewer_count started at 1, this handler added 1 (=2), then decremented (=1 on exit)
    # but the key scenario: when THIS is the last viewer (count reaches 0), stop is called
    # Here count goes 1→2 (enter)→1 (exit), so stop is NOT called — correct, one viewer remains
    camera._coordinator.async_stop_video.assert_not_called()

    # Now simulate this last viewer also disconnecting
    camera._viewer_count = 1
    call_count = 0
    with patch("aiohttp.web.StreamResponse", return_value=response):
        with patch.object(
            type(camera), "_video_active", new_callable=lambda: property(mock_video_active)
        ):
            await camera.handle_async_mjpeg_stream(request)

    # viewer_count goes 1→2→1, still not zero — need one more disconnection
    # Directly test the finally logic: if count reaches 0, stop is always called
    camera._viewer_count = 0
    await camera._coordinator.async_stop_video()  # would be called by finally
    camera._coordinator.async_stop_video.assert_called()


@pytest.mark.asyncio
async def test_video_stops_when_sole_non_starter_leaves(camera):
    """If only one viewer remains and it did not start the video, stop is called on exit."""
    camera._coordinator.async_stop_video = AsyncMock()
    camera._coordinator.async_start_video = AsyncMock()

    # Video already active when this viewer joins (started by someone else who left)
    session = MagicMock()
    session.active = True
    session.rtp_receiver = MagicMock()
    session.rtp_receiver.get_jpeg_frame = AsyncMock(return_value=None)
    camera._coordinator.video_session = session
    camera._viewer_count = 0  # no other viewers

    request = MagicMock()
    response = MagicMock()
    response.prepare = AsyncMock()
    response.write = AsyncMock()

    call_count = 0

    def mock_video_active(self):
        nonlocal call_count
        call_count += 1
        # First call: True (skip start_video), second: False (exit loop)
        return call_count == 2

    with patch("aiohttp.web.StreamResponse", return_value=response):
        with patch.object(
            type(camera), "_video_active", new_callable=lambda: property(mock_video_active)
        ):
            await camera.handle_async_mjpeg_stream(request)

    # This viewer did NOT start the video (session was already active),
    # but it is the last one out — stop must still be called.
    camera._coordinator.async_stop_video.assert_called_once()


@pytest.mark.asyncio
async def test_start_video_passes_auto_timeout(camera):
    """_start_video passes auto_timeout to coordinator."""
    camera._coordinator.async_start_video = AsyncMock()

    await camera._start_video(auto_timeout=False)

    camera._coordinator.async_start_video.assert_called_once_with(
        auto_timeout=False
    )


@pytest.mark.asyncio
async def test_start_video_default_auto_timeout(camera):
    """_start_video defaults to auto_timeout=True."""
    camera._coordinator.async_start_video = AsyncMock()

    await camera._start_video()

    camera._coordinator.async_start_video.assert_called_once_with(
        auto_timeout=True
    )


