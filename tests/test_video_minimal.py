"""Minimal video stream test — skip CTPP call signaling.

Tests whether the device will stream video with just:
  Auth → UDPM → UDP pings → RTPC → video config

Run with: python3 tests/test_video_minimal.py
"""

import asyncio
import struct
import sys
import os
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.comelit_intercom_local.auth import authenticate
from custom_components.comelit_intercom_local.channels import ChannelType
from custom_components.comelit_intercom_local.client import IconaBridgeClient
from custom_components.comelit_intercom_local.rtp_receiver import RtpReceiver, _build_control_packet

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(message)s")
_LOGGER = logging.getLogger(__name__)

HOST = os.environ.get("COMELIT_HOST", "192.168.1.111")
TOKEN = os.environ.get("COMELIT_TOKEN", "")
PORT = 64100
CALLEE = "SB100001"


class UdpListener(asyncio.DatagramProtocol):
    """Simple UDP listener that logs everything received."""

    def __init__(self):
        self.packets = 0

    def connection_made(self, transport):
        _LOGGER.info("UDP socket ready: %s", transport.get_extra_info("sockname"))

    def datagram_received(self, data, addr):
        self.packets += 1
        _LOGGER.info(
            "UDP RECEIVED #%d: %d bytes from %s:%d — first 20: %s",
            self.packets, len(data), addr[0], addr[1],
            data[:20].hex(" "),
        )

    def error_received(self, exc):
        _LOGGER.error("UDP error: %s", exc)


async def main():
    client = IconaBridgeClient(HOST, PORT)
    await client.connect()
    await authenticate(client, TOKEN)

    # Get config to know our apt address
    from custom_components.comelit_intercom_local.config_reader import get_device_config
    config = await get_device_config(client)
    apt_addr = config.apt_address
    apt_sub = config.apt_subaddress
    caller = f"{apt_addr}{apt_sub}"
    _LOGGER.info("Config: apt=%s, sub=%d, caller=%s", apt_addr, apt_sub, caller)

    # Step 1: Open CTPP + CSPB (device may need these)
    extra = f"{apt_addr}{apt_sub}"
    ctpp = await client.open_channel("CTPP", ChannelType.UAUT, extra_data=extra)
    await client.open_channel("CSPB", ChannelType.UAUT)
    _LOGGER.info("CTPP + CSPB opened")

    # Step 2: Open UDPM
    udpm = await client.open_channel("UDPM", ChannelType.UAUT, trailing_byte=1)
    udpm_token = 0
    if len(udpm.open_response_body) >= 18:
        udpm_token = struct.unpack_from("<H", udpm.open_response_body, 16)[0]
    _LOGGER.info(
        "UDPM opened: req_id=0x%04X, token=0x%04X, response=%s",
        udpm.request_id, udpm_token,
        udpm.open_response_body.hex(" "),
    )

    # Step 3: Start UDP socket and send 2 pings
    loop = asyncio.get_running_loop()
    listener = UdpListener()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: listener,
        remote_addr=(HOST, PORT),
    )
    local_addr = transport.get_extra_info("sockname")
    _LOGGER.info("UDP bound to local port %d", local_addr[1])

    # Send 2 pings matching PCAP format
    for seq in range(2):
        pkt = _build_control_packet(udpm.request_id, udpm_token, seq)
        transport.sendto(pkt)
        _LOGGER.info("Sent UDP ping %d: %s", seq, pkt.hex(" "))

    # Wait a moment for device UDP ACK
    await asyncio.sleep(1.0)
    _LOGGER.info("UDP packets received so far: %d", listener.packets)

    # Step 4: Open 2 RTPC channels
    rtpc1 = await client.open_channel("RTPC", ChannelType.UAUT, trailing_byte=1)
    rtpc2 = await client.open_channel(
        "RTPC2", ChannelType.UAUT, trailing_byte=1, wire_name="RTPC"
    )
    _LOGGER.info(
        "RTPC channels: rtpc1=0x%04X, rtpc2=0x%04X",
        rtpc1.request_id, rtpc2.request_id,
    )

    # Step 5: Send video config directly on CTPP (skip call signaling)
    from custom_components.comelit_intercom_local.protocol import encode_video_config
    import time
    ts = int(time.time()) & 0xFFFFFFFF
    vid_config = encode_video_config(caller, CALLEE, rtpc2.request_id, ts)
    await client.send_binary(ctpp, vid_config)
    _LOGGER.info("Sent video config on CTPP (ts=0x%08X)", ts)

    # Step 6: Wait and listen for UDP video
    _LOGGER.info("Waiting 30 seconds for UDP video data...")
    for i in range(30):
        await asyncio.sleep(1.0)
        if listener.packets > 0:
            _LOGGER.info("Got %d UDP packets after %d seconds!", listener.packets, i + 1)

    _LOGGER.info("Final: received %d UDP packets total", listener.packets)

    transport.close()
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
