"""Test: extract UDP video from PCAP and decode via RtpReceiver's pipeline."""

import struct
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scapy.all import rdpcap, UDP

PCAP_FILE = os.path.join(
    os.path.dirname(__file__), "..", "ComelitCalls", "sanitized_v2_1.pcap"
)

HEADER_SIZE = 8  # ICONA header size


def extract_udp_video_packets(pcap_path: str) -> list[bytes]:
    """Extract raw UDP payloads destined for the phone (video data)."""
    packets = rdpcap(pcap_path)
    video_pkts = []
    for pkt in packets:
        if not pkt.haslayer(UDP):
            continue
        raw = bytes(pkt[UDP].payload)
        if len(raw) < HEADER_SIZE + 12:
            continue
        # Check for ICONA header signature (first byte 0x00, second 0x06)
        if raw[0] != 0x00 or raw[1] != 0x06:
            continue
        # Get req_id — video packets have a specific req_id
        req_id = struct.unpack_from("<H", raw, 4)[0]
        # We'll collect all req_ids and find the most common one (video)
        video_pkts.append((req_id, raw))
    return video_pkts


def test_decode_pcap_video():
    """Decode H.264 from PCAP UDP packets using the same logic as RtpReceiver."""
    import av
    import io

    raw_packets = extract_udp_video_packets(PCAP_FILE)
    print(f"Total ICONA UDP packets: {len(raw_packets)}")

    # Find the most common req_id (that's the video stream)
    from collections import Counter
    req_counts = Counter(req_id for req_id, _ in raw_packets)
    print(f"Request IDs: {[(hex(k), v) for k, v in req_counts.most_common(5)]}")

    media_req_id = req_counts.most_common(1)[0][0]
    print(f"Using media req_id: 0x{media_req_id:04X}")

    # Filter to media packets only
    media_packets = [raw for req_id, raw in raw_packets if req_id == media_req_id]
    print(f"Media packets: {len(media_packets)}")

    # --- Replicate RtpReceiver logic ---
    current_fua_nal = bytearray()
    nals = []

    for data in media_packets:
        # Strip ICONA header using body_len
        body_len = struct.unpack_from("<H", data, 2)[0]
        raw_rtp = data[HEADER_SIZE:HEADER_SIZE + body_len]

        if len(raw_rtp) < 13:
            continue

        # RTP version check
        byte0 = raw_rtp[0]
        version = (byte0 >> 6) & 0x03
        if version != 2:
            continue

        nal_data = raw_rtp[12:]  # Skip 12-byte RTP header
        if not nal_data:
            continue

        nal_type = nal_data[0] & 0x1F

        if nal_type in (7, 8):
            # SPS or PPS
            nals.append(b"\x00\x00\x00\x01" + nal_data)
        elif nal_type == 28:
            # FU-A
            if len(nal_data) < 2:
                continue
            fu_indicator = nal_data[0]
            fu_header = nal_data[1]
            start_bit = (fu_header >> 7) & 1
            end_bit = (fu_header >> 6) & 1
            frag_type = fu_header & 0x1F
            nal_ref = fu_indicator & 0xE0

            if start_bit:
                reconstructed = bytes([nal_ref | frag_type])
                current_fua_nal = bytearray(
                    b"\x00\x00\x00\x01" + reconstructed + nal_data[2:]
                )
            elif current_fua_nal:
                current_fua_nal.extend(nal_data[2:])

            if end_bit and current_fua_nal:
                nals.append(bytes(current_fua_nal))
                current_fua_nal = bytearray()
        elif 1 <= nal_type <= 23:
            nals.append(b"\x00\x00\x00\x01" + nal_data)

    print(f"NALs extracted: {len(nals)}")

    # Count NAL types
    nal_types = Counter()
    for nal in nals:
        t = nal[4] & 0x1F
        nal_types[t] += 1
    print(f"NAL types: {dict(nal_types)}")

    # --- Decode with PyAV ---
    codec = av.CodecContext.create("h264", "r")
    frame_count = 0
    jpeg_sizes = []

    for nal in nals:
        packets = codec.parse(nal)
        for packet in packets:
            frames = codec.decode(packet)
            for frame in frames:
                frame_count += 1
                # Convert to JPEG
                jpeg_data = frame_to_jpeg(frame)
                if jpeg_data:
                    jpeg_sizes.append(len(jpeg_data))
                    if frame_count <= 3:
                        print(
                            f"  Frame {frame_count}: {frame.width}x{frame.height}, "
                            f"JPEG={len(jpeg_data)} bytes"
                        )

    print(f"\nTotal decoded frames: {frame_count}")
    if jpeg_sizes:
        print(
            f"JPEG sizes: min={min(jpeg_sizes)}, max={max(jpeg_sizes)}, "
            f"avg={sum(jpeg_sizes)//len(jpeg_sizes)}"
        )

    # Save first frame as proof
    if jpeg_sizes:
        # Re-decode to get first frame
        codec2 = av.CodecContext.create("h264", "r")
        for nal in nals:
            packets = codec2.parse(nal)
            for packet in packets:
                frames = codec2.decode(packet)
                for frame in frames:
                    jpeg = frame_to_jpeg(frame)
                    if jpeg:
                        out_path = os.path.join(
                            os.path.dirname(__file__), "..", "test_frame.jpg"
                        )
                        with open(out_path, "wb") as f:
                            f.write(jpeg)
                        print(f"\nFirst frame saved to: {out_path}")
                        assert frame_count > 0, "Should decode at least 1 frame"
                        assert frame.width > 0
                        assert frame.height > 0
                        return

    assert frame_count > 0, "Should have decoded at least one frame"


def frame_to_jpeg(frame) -> bytes | None:
    """Convert a PyAV VideoFrame to JPEG bytes (same as RtpReceiver._frame_to_jpeg)."""
    import av
    import io

    try:
        output = io.BytesIO()
        encoder = av.CodecContext.create("mjpeg", "w")
        encoder.width = frame.width
        encoder.height = frame.height
        encoder.pix_fmt = "yuvj420p"
        from fractions import Fraction
        encoder.time_base = frame.time_base or Fraction(1, 25)

        if frame.format.name != "yuvj420p":
            frame = frame.reformat(format="yuvj420p")

        packets = encoder.encode(frame)
        for pkt in packets:
            output.write(bytes(pkt))
        packets = encoder.encode(None)
        for pkt in packets:
            output.write(bytes(pkt))

        return output.getvalue() if output.tell() > 0 else None
    except Exception as e:
        print(f"JPEG encode error: {e}")
        return None


if __name__ == "__main__":
    test_decode_pcap_video()
