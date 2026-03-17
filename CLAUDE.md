# Comelit Local — Project Guide

## Overview

Home Assistant custom component for the **Comelit 6701W** WiFi video intercom. Communicates entirely locally via the **ICONA Bridge TCP protocol** on port 64100 — no cloud dependency.

## Project Structure

```
custom_components/comelit_intercom_local/
  __init__.py        — HA integration setup (async_setup_entry)
  config_flow.py     — UI config flow with auto token extraction
  coordinator.py     — DataUpdateCoordinator (manages TCP connection lifecycle)
  button.py          — Door open button entities
  camera.py          — Camera entities (video stream)
  event.py           — Doorbell ring / missed call event entities
  protocol.py        — Wire protocol: 8-byte header, message types, binary payloads
  channels.py        — Channel definitions (UAUT, UCFG, CTPP, PUSH)
  client.py          — AsyncIO TCP client for ICONA Bridge
  auth.py            — Authentication flow (UAUT channel)
  token.py           — Token extraction from device HTTP backup endpoint
  config_reader.py   — Device configuration retrieval (UCFG channel)
  door.py            — Door open sequence (CTPP channel, 6-step binary)
  push.py            — Push notification listener (PUSH channel)
  camera_utils.py    — Camera/RTSP URL discovery
  video_call.py      — Video call signaling (CTPP→UDPM→RTPC→link→config)
  rtp_receiver.py    — UDP RTP receiver: ICONA header→RTP→H.264 FU-A→PyAV→JPEG
  models.py          — Data models (Door, Camera, DeviceConfig, PushEvent)
  exceptions.py      — Custom exceptions
  const.py           — Constants (domain, platforms, defaults)

tests/
  test_protocol.py     — Unit tests for wire protocol
  test_client.py       — Unit tests for TCP client
  test_integration.py  — Integration tests (requires real device)
  test_ha_component.py — HA component tests
  conftest.py          — Shared fixtures

docs/
  code_review_2026_03_16.md  — Code review findings (4 critical, 7 major, 6 minor)
  fix_plan_2026_03_16.md     — Ordered fix plan: 7 batches, branches, tests per issue

postman/             — Postman collection documenting HTTP + TCP requests
```

## Setup & Development

**Requirements:** Python 3.11+, Home Assistant 2024.1+ (for HA integration)

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run unit tests (no device needed)
PYTHONPATH=. python3 -m pytest tests/test_protocol.py tests/test_client.py -v

# Run integration tests (requires real device on LAN)
COMELIT_HOST=192.168.31.201 COMELIT_TOKEN=<token> pytest tests/test_integration.py -v -s
```

## Testing Device

- IP: `192.168.31.201`, HTTP port: `8080`, ICONA port: `64100`
- Credentials: `admin` / `comelit`
- Token: stored in `.env` file (`COMELIT_TOKEN`)
- Config: apt_address=SB000006, apt_subaddress=1, 2 doors, 0 cameras

## ICONA Bridge Protocol

All communication is raw TCP on port **64100**. Every message has an 8-byte header:

```
[0x00 0x06] [body_length LE16] [request_id LE16] [0x00 0x00]
```

### Channels and Flow

1. **UAUT** — Authentication: open channel → send JSON access request with token → expect code 200
2. **UCFG** — Configuration: request config → parse doors, cameras, apt_address
3. **PUSH** — Notifications: receive unsolicited JSON on doorbell_ring / missed_call
4. **CTPP** — Door control: 6-step binary sequence on a fresh TCP connection
5. **UDPM/RTPC** — Video call signaling (uses `trailing_byte=1`)

### Critical Protocol Rules

- **Channel open sequence must always be 1** — device ignores packets with seq != 1
- **Timeout must be >= 30s** — device can be very slow to respond
- **Request ID** starts semi-random (8000+) and increments per message
- After channel open, server responds with `server_channel_id` used for subsequent messages
- JSON messages use compact format: `separators=(",", ":")`
- Door open uses a **fresh TCP connection** (not the main persistent one)

### CTPP "Timestamp" Field = Session ID (KEY DISCOVERY)

Bytes [2-5] in CTPP messages are NOT just timestamps:
- Bytes [2-3] = **session ID** (LE16) — MUST be consistent within a phase
- Bytes [4-5] = counter (LE16) — increments per message
- Init phase uses one session ID, call phase uses a different one
- ACKs MUST have same session bytes as the message they're ACKing

### PCAP Address Protocol

- CTPP channel open extra_data = `our_addr` (apt_addr + apt_sub, e.g. "SB0000062")
- CTPP init body: pos1=our_addr, pos2=our_addr, pos3=apt_addr_base (no subaddress)
- Init ACKs (0x1800/0x1820): first=our_addr, second=apt_addr_base (WITHOUT subaddress)
- Call init/codec/RTPC/video messages: first=our_addr, second=entrance_addr
- entrance_addr from `entrance-address-book` in device config (= "SB100001" in PCAP)
- **ALL CTPP video messages put our_addr FIRST** — entrance/apt_base SECOND

### Codec Exchange Protocol (PCAP-verified, final)

1. We send: 0x1840 action=0x0008 (our codec params)
2. Device ACKs: 0x1800 ← skip in loop
3. Device sends: 0x1840 action=0x0008 (device codec params)
4. We ACK: bare 0x1800 (`encode_call_response_ack`)
5. Device sends: 0x1840 action=0x0002 ("call accepted")
6. We ACK: 0x1800 → break loop

### Device RTPC Timeline

- Device opens RTPC during codec exchange (~4.8s after connect), BEFORE we open RTPC1/RTPC2
- If codec exchange doesn't complete within ~15s, device sends 0x01EF END and closes
- `register_placeholder_channel("RTPC_DEVICE")` must be called BEFORE call_init

### Channel Open Count: PCAP vs Our Code

- PCAP opens: UAUT, UCFG, INFO, CTPP, CSPB, FRCG, 2nd-UAUT, PUSH, 2nd-UCFG, UDPM, RTPC×2
- Our code opens: UAUT (auth only), CTPP, CSPB, UDPM, RTPC×2
- Missing: INFO, FRCG — may not be required for video but could affect device state

## Key Entities

| Entity | Description |
|--------|-------------|
| `button.<door_name>` | Press to open door/gate |
| `event.doorbell` | Fires `doorbell_ring` and `missed_call` events |
| `camera.intercom_video` | Live video stream from intercom |
| `button.start_video` | Manually trigger video call |

### Entity ID Note

Door `id` from device can be non-unique (e.g., both doors had id=0). The `index` field on the Door model is a sequential counter used for unique entity IDs.

## Video Streaming

- `video_call.py` — TCP signaling: CTPP → call init → UDPM → codec → 2x RTPC → link → video config
- `rtp_receiver.py` — UDP reception: ICONA header → strip trailer via body_len → RTP → H.264 FU-A → PyAV → JPEG
- UDPM/RTPC channels use `trailing_byte=1`, all channels use ChannelType.UAUT (type=7)
- Video config sends 800x480 main + 320x240 secondary + 16 FPS
- Auto-starts on `doorbell_ring` push event; manual start via "Start Video" button
- Auto-timeout after 120 seconds
- **Requires `av` package** (`pip install av`) on HA system
- Neither reference repo (nicolas-fricke, madchicken) has video streaming — original work

### UDP Video Format (PCAP-verified)

- No XOR/S-Box encryption — video is standard H.264 via RTP
- UDP packets: 8-byte ICONA header + RTP data + ~34-byte "Comelit" trailer
- Trailer: `00 00 01 07 20 21 00 00 28 8e 43 6f 6d 65 6c 69 74 00...00 [4 bytes]`
- ICONA header `body_len` gives real RTP data size — trim trailer using this value
- SPS `67 42 00 1f` = H.264 Baseline profile, PPS `68 ce 38 80`
- FU-A (NAL type 28) used for large frames; 1342-byte packets typical
- UDP control req_id = `udpm.server_channel_id` (device-assigned, e.g. 0x606C)
- UDP media req_id = `rtpc2.server_channel_id` (device-assigned, e.g. 0x606E = control + 2)
- Keepalive: send 1 UDP control packet every ~1.5s for entire session

### Native Library Findings (libcomelitvipkit.so)

- Capabilities bitmask: AUDIO_DST=1, AUDIO_SRC=2, VIDEO_DST=4, VIDEO_SRC=8, OPENDOOR=16, MSTREAM=32
- `0x0040` at bytes 8-9 in CTPP init body is likely the encoded capability bitfield
- "Comelit" string appears only in UDP video packet trailers, not in TCP handshake

### PCAP Reference

- `ComelitCalls/sanitized_v2_1.pcap` — sanitized PCAP from real Android app
- 1051 video packets with req_id=0x606E in the PCAP

## HA Debug Logging

```yaml
logger:
  default: info
  logs:
    custom_components.comelit_intercom_local: debug
```

## Workflow Preferences

- **Use expert agents (subagents) whenever possible** — delegate research, code exploration, and independent subtasks to subagents to parallelize work and keep the main context clean.
- **Run and test code yourself** — don't ask user to run things manually. User runs Claude Code non-sandboxed with LAN access to device.
- **Persistent notes go in `docs/` or `CLAUDE.md`** — `/home/agent/.claude/` is container-local and ephemeral; never use it for anything that needs to survive a session restart.

## Coding Conventions

- AsyncIO throughout — all network I/O is async
- Protocol encoding/decoding lives in `protocol.py`; business logic in channel-specific modules
- Compact JSON serialization (`separators=(",",":")`) for all messages to device
- Exceptions defined in `exceptions.py` — use these rather than generic exceptions
- pytest with `asyncio_mode = "auto"` — async test functions work without decorator
- Target Python 3.11+ — avoid PEP 695 `type X = ...` syntax (requires 3.12); use `TypeAlias` from `typing`

## Device Behavior & Quirks

### Network & Power

- The intercom **disconnects from WiFi when idle** — turns off after ~10-20 seconds of inactivity. Must physically wake it (tap a button) before any network test.
- Open ports: **53** (DNS), **8080** (HTTP), **8443** (HTTPS, bad cert), **64100** (ICONA protocol)
- Port 8080 serves admin page (`admin`/`comelit`) — device info page shows UUID and 32-char hex token (ID32 token used for auth)

### Protocol Discovery

- Port 64100 does **not** speak HTTP — custom binary+JSON protocol over raw TCP and UDP
- First 2 bytes of header always `0x00 0x06`
- Body length: `body_length = byte2 + (byte3 * 256)` (little-endian 16-bit)
- UDP packet with `INFO` to port **24199** returns hardware info (MAC address, etc.)

### Channel Open Sequence

1. Open TCP stream to port 64100
2. Open a channel — 23-byte packet: 8-byte header + 8-byte magic prefix (`cd ab 01 00 07 00 00 00`) + channel name (4 bytes) + 3 trailing bytes
3. Send command — JSON body prefixed with 8-byte header containing the channel ID bytes

### Door Control

- Door opening uses **binary-only** CTPP/CSPB channel commands, not JSON
- Requires a fresh TCP connection with 6-step binary sequence

### Cloud Architecture (not used)

- Official app routes through Google Cloud MQTT + Vultr STUN/TURN for NAT traversal
- `sbc.pm-always-on: false` → device sleeps when idle
- This component bypasses all cloud infrastructure

## Reference

- [ha-component-comelit-intercom](https://github.com/nicolas-fricke/ha-component-comelit-intercom) — Nicolas Fricke
- [comelit-client](https://github.com/madchicken/comelit-client) — Pierpaolo Follia
- [Protocol analysis Part 1](https://grdw.nl/2023/01/28/my-intercom-part-1.html) — grdw (reverse engineering the ICONA protocol)
