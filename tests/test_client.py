"""Client tests with mocked TCP connection."""

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.client import IconaBridgeClient
from custom_components.comelit_intercom_local.protocol import (
    HEADER_SIZE,
    MessageType,
    encode_header,
)
from custom_components.comelit_intercom_local.channels import ChannelType


def _make_command_response(server_channel_id: int, sequence: int = 2) -> bytes:
    """Build a raw COMMAND response packet (header + body)."""
    body = bytearray(10)
    struct.pack_into("<H", body, 0, MessageType.COMMAND)
    struct.pack_into("<H", body, 2, sequence)
    struct.pack_into("<I", body, 4, 0)
    struct.pack_into("<H", body, 8, server_channel_id)
    return encode_header(len(body), 0) + bytes(body)


def _make_json_response(channel_id: int, payload: dict) -> bytes:
    """Build a raw JSON response packet."""
    import json

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return encode_header(len(body), channel_id) + body


class FakeStreamReader:
    """Simulates asyncio.StreamReader with queued data."""

    def __init__(self):
        self._buffer = bytearray()

    def feed(self, data: bytes):
        self._buffer.extend(data)

    async def readexactly(self, n: int) -> bytes:
        # Wait until enough data is available
        for _ in range(100):
            if len(self._buffer) >= n:
                result = bytes(self._buffer[:n])
                del self._buffer[:n]
                return result
            await asyncio.sleep(0.01)
        raise asyncio.IncompleteReadError(bytes(self._buffer), n)


class FakeStreamWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_open_channel():
    """Test that open_channel sends COMMAND and receives server channel ID."""
    reader = FakeStreamReader()
    writer = FakeStreamWriter()

    client = IconaBridgeClient("127.0.0.1")
    client._reader = reader
    client._writer = writer
    client._connected = True
    client._receive_task = asyncio.create_task(client._receive_loop())

    # Feed the command response (server assigns channel id 42)
    reader.feed(_make_command_response(server_channel_id=42))

    try:
        channel = await asyncio.wait_for(
            client.open_channel("UAUT", ChannelType.UAUT), timeout=3.0
        )
        assert channel.is_open
        assert channel.server_channel_id == 42
        assert channel.name == "UAUT"
    finally:
        client._connected = False
        client._receive_task.cancel()
        try:
            await client._receive_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_send_json_and_receive():
    """Test sending JSON on a channel and receiving a JSON response."""
    reader = FakeStreamReader()
    writer = FakeStreamWriter()

    client = IconaBridgeClient("127.0.0.1")
    client._reader = reader
    client._writer = writer
    client._connected = True
    client._receive_task = asyncio.create_task(client._receive_loop())

    # Feed command response to open channel
    reader.feed(_make_command_response(server_channel_id=100))

    try:
        channel = await asyncio.wait_for(
            client.open_channel("UAUT", ChannelType.UAUT), timeout=3.0
        )

        # Now feed a JSON response for the data exchange
        response_payload = {"message": "access", "response-code": 200, "response-string": "OK"}
        reader.feed(_make_json_response(100, response_payload))

        result = await asyncio.wait_for(
            client.send_json(channel, {"message": "access", "user-token": "test"}),
            timeout=3.0,
        )
        assert result["response-code"] == 200
    finally:
        client._connected = False
        client._receive_task.cancel()
        try:
            await client._receive_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_push_callback():
    """Test that unsolicited JSON messages trigger the push callback."""
    reader = FakeStreamReader()
    writer = FakeStreamWriter()

    client = IconaBridgeClient("127.0.0.1")
    client._reader = reader
    client._writer = writer
    client._connected = True

    received = []
    client.set_push_callback(lambda msg: received.append(msg))

    client._receive_task = asyncio.create_task(client._receive_loop())

    try:
        # Feed an unsolicited JSON message on channel_id 999 (no pending callback)
        push_msg = {"event": "doorbell", "apt-address": "00000001"}
        reader.feed(_make_json_response(999, push_msg))

        await asyncio.sleep(0.5)
        assert len(received) == 1
        assert received[0]["event"] == "doorbell"
    finally:
        client._connected = False
        client._receive_task.cancel()
        try:
            await client._receive_task
        except asyncio.CancelledError:
            pass
