"""Unit tests for RtpReceiver — no device or PyAV needed."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.rtp_receiver import RtpReceiver


class TestRtpReceiverStop:
    @pytest.mark.asyncio
    async def test_stop_awaits_keepalive_task(self):
        """stop() must await the keepalive task, not just cancel it."""
        receiver = RtpReceiver("127.0.0.1")
        receiver._running = True

        cancelled = asyncio.Event()

        async def slow_keepalive():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        receiver._keepalive_task = asyncio.create_task(slow_keepalive())
        await asyncio.sleep(0)  # let the task start before cancelling

        await receiver.stop()

        assert cancelled.is_set(), "keepalive task was not properly awaited/cancelled"
        assert receiver._keepalive_task is None

    @pytest.mark.asyncio
    async def test_stop_awaits_decode_task(self):
        """stop() must await the decode task, not just cancel it."""
        receiver = RtpReceiver("127.0.0.1")
        receiver._running = True

        cancelled = asyncio.Event()

        async def slow_decode():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        receiver._decode_task = asyncio.create_task(slow_decode())
        await asyncio.sleep(0)  # let the task start before cancelling

        await receiver.stop()

        assert cancelled.is_set(), "decode task was not properly awaited/cancelled"
        assert receiver._decode_task is None

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self):
        receiver = RtpReceiver("127.0.0.1")
        receiver._running = True
        await receiver.stop()
        assert not receiver._running

    @pytest.mark.asyncio
    async def test_running_property(self):
        receiver = RtpReceiver("127.0.0.1")
        assert not receiver.running
        receiver._running = True
        assert receiver.running


class TestDecodeLoopRobustness:
    def _make_fake_av(self, *, parse_raises=None, decode_raises=None) -> ModuleType:
        """Build a minimal fake `av` module for injection."""
        fake_av = ModuleType("av")

        class FakeInvalidDataError(Exception):
            pass

        fake_av.error = ModuleType("av.error")
        fake_av.error.InvalidDataError = FakeInvalidDataError

        packet = MagicMock()

        class FakeCodecContext:
            def parse(self, data):
                if parse_raises:
                    raise parse_raises
                return [packet]

            def decode(self, pkt):
                if decode_raises:
                    raise decode_raises
                return []

        fake_av.CodecContext = MagicMock()
        fake_av.CodecContext.create = lambda *a, **kw: FakeCodecContext()
        return fake_av

    @pytest.mark.asyncio
    async def test_decode_loop_stops_on_repeated_errors(self):
        """_decode_loop must break after _MAX_CONSECUTIVE_ERRORS non-InvalidDataError exceptions."""
        receiver = RtpReceiver("127.0.0.1")
        receiver._running = True

        fake_av = self._make_fake_av(decode_raises=RuntimeError("boom"))

        with patch.dict(sys.modules, {"av": fake_av, "av.error": fake_av.error}):
            for _ in range(10):
                await receiver._nal_queue.put(b"\x00\x00\x00\x01\x65" + b"\x00" * 20)

            await receiver._decode_loop()

        # Loop exited after 5 consecutive errors without hanging

    @pytest.mark.asyncio
    async def test_decode_loop_continues_on_invalid_data(self):
        """InvalidDataError must reset the consecutive error counter and not stop the loop."""
        receiver = RtpReceiver("127.0.0.1")
        receiver._running = True

        fake_av = self._make_fake_av()
        parse_call_count = 0

        class FakeCodecContext:
            def parse(self, data):
                nonlocal parse_call_count
                parse_call_count += 1
                if parse_call_count <= 3:
                    raise fake_av.error.InvalidDataError("bad data")
                receiver._running = False
                return []

            def decode(self, pkt):
                return []

        fake_av.CodecContext.create = lambda *a, **kw: FakeCodecContext()

        with patch.dict(sys.modules, {"av": fake_av, "av.error": fake_av.error}):
            for _ in range(10):
                await receiver._nal_queue.put(b"\x00\x00\x00\x01\x65" + b"\x00" * 20)
            await receiver._decode_loop()

        assert parse_call_count >= 3
