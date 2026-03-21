"""Unit tests for protocol encoding/decoding — no device needed."""

import struct

from custom_components.comelit_intercom_local.protocol import (
    HEADER_MAGIC,
    HEADER_SIZE,
    MessageType,
    _CTPP_LEGACY_TS,
    decode_header,
    encode_channel_close,
    encode_channel_open,
    encode_ctpp_init,
    encode_door_init,
    encode_header,
    encode_json_message,
    encode_open_door,
    encode_rtpc_link,
    encode_video_config_resp,
    is_json_body,
    parse_command_response,
)
from custom_components.comelit_intercom_local.channels import ChannelType


class TestHeader:
    def test_encode_header_magic(self):
        h = encode_header(0, 0)
        assert h[:2] == HEADER_MAGIC

    def test_encode_header_length(self):
        assert len(encode_header(100, 5)) == HEADER_SIZE

    def test_encode_decode_roundtrip(self):
        h = encode_header(1234, 42)
        body_len, req_id = decode_header(h)
        assert body_len == 1234
        assert req_id == 42

    def test_decode_header_too_short(self):
        import pytest

        with pytest.raises(ValueError):
            decode_header(b"\x00\x06\x00")

    def test_header_padding_zero(self):
        h = encode_header(10, 20)
        assert h[6:8] == b"\x00\x00"


class TestJsonMessage:
    def test_encode_json_message(self):
        msg = {"message": "access", "user-token": "abc123"}
        packet = encode_json_message(msg, request_id=8001)
        header = packet[:HEADER_SIZE]
        body = packet[HEADER_SIZE:]
        body_len, req_id = decode_header(header)
        assert req_id == 8001
        assert body_len == len(body)
        assert b'"message":"access"' in body  # compact JSON

    def test_is_json_body(self):
        assert is_json_body(b'{"message":"ok"}')
        assert not is_json_body(b"\xc0\x18\x5c")
        assert not is_json_body(b"")


class TestChannelOpen:
    def test_encode_channel_open_basic(self):
        packet = encode_channel_open("UAUT", ChannelType.UAUT, sequence=1, request_id=8001)
        # header should have request_id=0 (binary command)
        _, req_id = decode_header(packet[:HEADER_SIZE])
        assert req_id == 0
        body = packet[HEADER_SIZE:]
        # first 2 bytes: COMMAND type
        msg_type = struct.unpack_from("<H", body, 0)[0]
        assert msg_type == MessageType.COMMAND
        # next 2 bytes: sequence
        seq = struct.unpack_from("<H", body, 2)[0]
        assert seq == 1
        # next 4 bytes: channel type id
        ch_type = struct.unpack_from("<I", body, 4)[0]
        assert ch_type == ChannelType.UAUT

    def test_encode_channel_open_with_extra_data(self):
        packet = encode_channel_open(
            "CTPP", ChannelType.CTPP, sequence=1, request_id=8001, extra_data="000000010"
        )
        body = packet[HEADER_SIZE:]
        # extra_data should appear somewhere in the body
        assert b"000000010\x00" in body

    def test_encode_channel_close(self):
        packet = encode_channel_close(sequence=3)
        _, req_id = decode_header(packet[:HEADER_SIZE])
        assert req_id == 0
        body = packet[HEADER_SIZE:]
        msg_type = struct.unpack_from("<H", body, 0)[0]
        assert msg_type == MessageType.END
        seq = struct.unpack_from("<H", body, 2)[0]
        assert seq == 3


class TestCommandResponse:
    def test_parse_command_response(self):
        body = bytearray(10)
        struct.pack_into("<H", body, 0, MessageType.COMMAND)
        struct.pack_into("<H", body, 2, 2)  # sequence
        struct.pack_into("<I", body, 4, 0)  # value
        struct.pack_into("<H", body, 8, 42)  # server channel id
        msg_type, seq, ch_id = parse_command_response(bytes(body))
        assert msg_type == MessageType.COMMAND
        assert seq == 2
        assert ch_id == 42


class TestDoorPayloads:
    def test_ctpp_init_contains_address(self):
        payload = encode_ctpp_init("00000001", 0)
        assert b"000000010\x00" in payload
        assert b"00000001\x00" in payload
        # starts with expected magic bytes
        assert payload[:4] == bytes([0xC0, 0x18, 0x5C, 0x8B])

    def test_open_door_message(self):
        payload = encode_open_door(
            MessageType.OPEN_DOOR, "00000001", 1, "00000000"
        )
        # starts with OPEN_DOOR type LE
        assert payload[:2] == struct.pack("<H", MessageType.OPEN_DOOR)
        assert b"000000011\x00" in payload  # apt_address + output_index
        assert b"00000000\x00" in payload  # door_apt_address

    def test_open_door_confirm_message(self):
        payload = encode_open_door(
            MessageType.OPEN_DOOR_CONFIRM, "00000001", 1, "00000000"
        )
        assert payload[:2] == struct.pack("<H", MessageType.OPEN_DOOR_CONFIRM)

    def test_door_init_contains_output_index(self):
        payload = encode_door_init("00000001", 1, "00000000")
        assert payload[:4] == bytes([0xC0, 0x18, 0x70, 0xAB])
        # output_index as LE uint32
        assert struct.pack("<I", 1) in payload

    def test_ctpp_init_with_timestamp_differs_from_legacy(self):
        """encode_ctpp_init with a timestamp must differ from the legacy hardcoded one."""
        with_ts = encode_ctpp_init("SB000006", 1, timestamp=0x12345678)
        legacy = encode_ctpp_init("SB000006", 1)
        # the timestamp bytes at positions 2-5 must differ
        assert with_ts[2:6] != legacy[2:6]
        # legacy payload must embed _CTPP_LEGACY_TS
        assert _CTPP_LEGACY_TS in legacy


class TestVideoPayloads:
    def test_encode_rtpc_link_normal_first_byte(self):
        """encode_rtpc_link without refresh=True uses 0x18 as first extra byte."""
        msg = encode_rtpc_link("SB0000061", "SB100001", 0x21B5, 0x12345678)
        # extra starts at byte 8 (after prefix+timestamp+action+flags = 2+4+2+2)
        # first byte of extra should be 0x18
        assert bytes([0x18, 0x02]) in msg

    def test_encode_rtpc_link_refresh_first_byte(self):
        """encode_rtpc_link with refresh=True uses 0x98 as first extra byte."""
        msg = encode_rtpc_link("SB0000061", "SB100001", 0x21B5, 0x12345678, refresh=True)
        assert bytes([0x98, 0x02]) in msg
        assert bytes([0x18, 0x02]) not in msg

    def test_encode_video_config_resp_structure(self):
        """encode_video_config_resp uses 0x1860 prefix and action 0x001A."""
        msg = encode_video_config_resp("SB0000061", "SB100001", 0x21B6, 0x12345678)
        prefix = struct.unpack_from("<H", msg, 0)[0]
        assert prefix == 0x1860
        action = struct.unpack_from(">H", msg, 6)[0]
        assert action == 0x001A  # ACTION_VIDEO_CONFIG

    def test_encode_video_config_resp_contains_rtpc2_id(self):
        """encode_video_config_resp embeds rtpc2_req_id in the extra block."""
        msg = encode_video_config_resp("SB0000061", "SB100001", 0xABCD, 0x12345678)
        assert struct.pack("<H", 0xABCD) in msg

    def test_encode_video_config_resp_no_resolution(self):
        """encode_video_config_resp extra block has zeros only (no resolution/fps)."""
        msg = encode_video_config_resp("SB0000061", "SB100001", 0x21B6, 0x12345678)
        # Should NOT contain the 800x480 resolution from encode_video_config
        assert struct.pack("<H", 800) not in msg
        assert struct.pack("<H", 480) not in msg
