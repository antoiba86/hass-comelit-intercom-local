"""Client tests with mocked TCP connection."""

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.comelit_intercom_local.client import IconaBridgeClient
from custom_components.comelit_intercom_local.exceptions import ConnectionComelitError
from custom_components.comelit_intercom_local.protocol import (
    HEADER_SIZE,
    MessageType,
    decode_header,
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
    # Yield once so the receive loop starts and enters its polling state before
    # we feed data. On Python 3.11, wait_for() wraps the coroutine in a new
    # task, so without this yield the receive loop can run first and consume
    # the response before open_channel has registered the channel.
    await asyncio.sleep(0)

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
    """Test sending JSON on a channel and receiving a JSON response.

    send_json uses a unique per-message request_id (not server_channel_id), so
    we must extract that ID from the written packet and echo it back.
    """
    reader = FakeStreamReader()
    writer = FakeStreamWriter()

    client = IconaBridgeClient("127.0.0.1")
    client._reader = reader
    client._writer = writer
    client._connected = True
    client._receive_task = asyncio.create_task(client._receive_loop())
    await asyncio.sleep(0)  # let receive loop enter polling state (Python 3.11 compat)

    # Feed command response to open channel
    reader.feed(_make_command_response(server_channel_id=100))

    try:
        channel = await asyncio.wait_for(
            client.open_channel("UAUT", ChannelType.UAUT), timeout=3.0
        )

        # Device responds with server_channel_id (100) as the request_id
        response_payload = {"message": "access", "response-code": 200, "response-string": "OK"}
        reader.feed(_make_json_response(100, response_payload))

        # Start send_json as a background task
        send_task = asyncio.create_task(
            client.send_json(channel, {"message": "access", "user-token": "test"})
        )

        # Wait for the packet to be written, then extract the msg_request_id
        for _ in range(50):
            if len(writer.data) >= HEADER_SIZE:
                break
            await asyncio.sleep(0.01)

        _, msg_request_id = decode_header(bytes(writer.data[:HEADER_SIZE]))

        # Feed a JSON response keyed by the actual msg_request_id
        response_payload = {"message": "access", "response-code": 200, "response-string": "OK"}
        reader.feed(_make_json_response(msg_request_id, response_payload))

        result = await asyncio.wait_for(send_task, timeout=3.0)
        assert result["response-code"] == 200
    finally:
        client._connected = False
        client._receive_task.cancel()
        try:
            await client._receive_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_concurrent_send_json_on_same_channel():
    """Concurrent send_json calls on the same channel are serialized by the lock.

    The lock ensures only one send is in-flight at a time. We verify this by
    holding the lock manually and checking that a concurrent send_json blocks
    until the lock is released.
    """
    reader = FakeStreamReader()
    writer = FakeStreamWriter()

    client = IconaBridgeClient("127.0.0.1")
    client._reader = reader
    client._writer = writer
    client._connected = True
    client._receive_task = asyncio.create_task(client._receive_loop())
    await asyncio.sleep(0)  # let receive loop enter polling state (Python 3.11 compat)

    reader.feed(_make_command_response(server_channel_id=100))

    try:
        channel = await asyncio.wait_for(
            client.open_channel("UAUT", ChannelType.UAUT), timeout=3.0
        )

        # Hold the lock manually — send_json must block
        await channel.send_lock.acquire()
        blocked_task = asyncio.create_task(
            client.send_json(channel, {"req": "blocked"})
        )
        await asyncio.sleep(0.05)
        assert not blocked_task.done(), "send_json should block while lock is held"

        # Release lock — yield so send_json can acquire it and register its
        # callback before we feed the response.
        channel.send_lock.release()
        await asyncio.sleep(0)
        reader.feed(_make_json_response(100, {"response-code": 200}))
        result = await asyncio.wait_for(blocked_task, timeout=3.0)
        assert result["response-code"] == 200
    finally:
        client._connected = False
        client._receive_task.cancel()
        try:
            await client._receive_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_send_raises_connection_error_on_drain_failure():
    """_send must raise ConnectionComelitError when drain raises OSError."""
    reader = FakeStreamReader()

    class FailingWriter(FakeStreamWriter):
        async def drain(self):
            raise OSError("broken pipe")

    writer = FailingWriter()
    client = IconaBridgeClient("127.0.0.1")
    client._reader = reader
    client._writer = writer
    client._connected = True

    with pytest.raises(ConnectionComelitError, match="Send failed"):
        await client._send(b"\x00\x06\x00\x00\x00\x00\x00\x00")


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
