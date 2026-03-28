# Comelit Local — Project Guide

## Overview

Home Assistant custom component for the **Comelit 6701W** WiFi video intercom. Communicates entirely locally via the **ICONA Bridge TCP protocol** on port 64100 — no cloud dependency.

## Project Structure

```
custom_components/comelit_local/
  __init__.py        — HA integration setup (async_setup_entry)
  config_flow.py     — UI config flow with auto token extraction
  coordinator.py     — DataUpdateCoordinator (manages TCP connection + persistent RTSP server)
  button.py          — Door open button entities
  camera.py          — Camera entities (video stream; stream_source always returns RTSP URL)
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
  video_call.py      — Video call signaling + answer sequence + inline re-establishment
  rtp_receiver.py    — UDP/TCP RTP receiver: H.264 FU-A→PyAV→JPEG + PCMA audio routing
  rtsp_server.py     — Local RTSP server: H.264 + PCMA → go2rtc → WebRTC (multi-client)
  models.py          — Data models (Door, Camera, DeviceConfig, PushEvent)
  exceptions.py      — Custom exceptions
  const.py           — Constants (domain, platforms, defaults)

tests/
  test_protocol.py   — Unit tests for wire protocol
  test_client.py     — Unit tests for TCP client
  test_integration.py — Integration tests (requires real device)
  test_ha_component.py — HA component tests
  conftest.py        — Shared fixtures

postman/             — Postman collection documenting HTTP + TCP requests
```

## Setup & Development

**Requirements:** Python 3.11+, Home Assistant 2024.1+ (for HA integration)

**Always use `uv` for Python** — never use `pip` or `python3` directly.

```bash
# Install dev dependencies
uv pip install -e ".[dev]"

# Run unit tests (no device needed)
PYTHONPATH=. uv run python -m pytest tests/test_protocol.py tests/test_client.py -v

# Run integration tests (requires real device on LAN)
COMELIT_HOST=192.168.31.201 COMELIT_TOKEN=<token> uv run python -m pytest tests/test_integration.py -v -s
```

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

- `video_call.py` handles TCP signaling: CTPP → call init → UDPM → codec → 2x RTPC → link → video config
- `rtp_receiver.py` handles UDP reception: ICONA header → RTP → H.264 FU-A → PyAV decode → JPEG
- `rtsp_server.py` serves H.264 + G.711 PCMA over local RTSP (TCP interleaved) for go2rtc → WebRTC
- Video config sends resolution 800x480 at 16 FPS
- Auto-starts on `doorbell_ring` push event; manual start via "Start Video" button
- **Persistent RTSP server** owned by coordinator — started at HA setup, never stopped between calls; `stream_source()` always returns a valid URL
- **Inline re-establishment** on CALL_END (~30s): ACK → refresh RTPC_LINK → VIDEO_CONFIG_RESP — no TCP reconnect, video is uninterrupted

## Audio Streaming (implemented 2026-03-25)

- Audio does **NOT** start automatically — device requires an explicit "answer" sequence after video starts
- **Answer sequence** (sent as background task after video is flowing, non-fatal):
  1. `encode_answer_video_reconfig` — prefix `0x183C`, resends 800×480 @ 16fps
  2. `encode_answer_peer` — prefix `0x1830`, action `0x70`, carries `apt_subaddress`
  3. `encode_answer_config_ack` — prefix `0x180C`, action `0x0C`
- Device responds by opening a new RTPC channel; audio flows ~3.5s later
- **Audio codec: PCMA G.711 A-law, PT=8, 20ms frames (160 bytes/frame)**
- Audio arrives on same UDP port as video, distinguished by RTP payload type (PT=8)
- `rtsp_server.py` sends silent PCMA keepalive (0xD5) every ~1s when no audio queued — keeps go2rtc alive
- **Hangup:** `encode_hangup` in `protocol.py`, action `0x2d` + entrance address
- See `docs/audio_protocol_findings_2026_03_22.md` for protocol analysis
- See `docs/implementation_state_2026_03_25.md` for full implementation notes

## Testing Device

- IP: `192.168.31.201`, HTTP port: `8080`, ICONA port: `64100`
- Credentials: `admin` / `comelit`
- Config: apt_address=SB000006, apt_subaddress=1, 2 doors, 0 cameras

## HA Debug Logging

```yaml
logger:
  default: info
  logs:
    custom_components.comelit_local: debug
```

## Workflow Preferences

- **Use expert agents (subagents) whenever possible** — delegate research, code exploration, and independent subtasks to subagents to parallelize work and keep the main context clean.

## Coding Conventions

- AsyncIO throughout — all network I/O is async
- Protocol encoding/decoding lives in `protocol.py`; business logic in channel-specific modules
- Compact JSON serialization (`separators=(",",":")`) for all messages to device
- Exceptions defined in `exceptions.py` — use these rather than generic exceptions
- pytest with `asyncio_mode = "auto"` — async test functions work without decorator

## Device Behavior & Quirks (from GRDW reverse-engineering)

### Network & Power

- The intercom **disconnects from WiFi when idle** — it turns off after ~10-20 seconds of inactivity and disappears from the router. You must physically wake it (tap a button) before any network test.
- Open ports: **53** (DNS), **8080** (HTTP), **8443** (HTTPS, bad cert), **64100** (ICONA protocol)
- Port 8080/8443 serves an "Extender - Index" admin page (default password: `admin`) with device info, reboot, and password change options. The device info page shows a UUID and a 32-char hex token (the ID32 token used for auth).

### Protocol Discovery

- Port 64100 does **not** speak HTTP — it's a custom binary+JSON protocol over raw TCP and UDP.
- The first 2 bytes of the header are always `0x00 0x06`.
- Body length encoding in header bytes 2-3: `body_length = byte2 + (byte3 * 256)` (little-endian 16-bit).
- Sending a UDP packet with `INFO` to port **24199** returns hardware info (MAC address, etc.) — this is from the NPM comelit-client discovery.

### Channel Open Sequence

The protocol works in 3 steps:

1. **Open TCP stream** to port 64100
2. **Open a channel** — sends a 23-byte packet:
   - 8-byte header: `00 06 0f 00 00 00 00 00`
   - 8-byte magic prefix: `cd ab 01 00 07 00 00 00`
   - Channel name (e.g., `UAUT` = `55 41 55 54`)
   - 3 trailing bytes: `[channel_id_byte] [channel_id_byte2] 00`
3. **Send command** over the opened channel — JSON body prefixed with 8-byte header containing the channel ID bytes from step 2

### Authentication (UAUT)

- After opening UAUT channel, send a JSON access request containing the 32-char hex token
- Success response: `{"message":"access","message-type":"response","message-id":1,"response-code":200,"response-string":"Access Granted"}`
- The token is the ID32 value from the device info page at port 8080

### Configuration Response (UCFG)

The `get-configuration` response includes:
- `viper-server`: local IP, TCP/UDP ports (64100)
- `viper-p2p.mqtt`: cloud MQTT server (unused for local control)
- `viper-p2p.stun`: STUN/TURN servers for remote access
- `vip`: apartment address (`apt-address`), sub-address, call-divert settings
- `building-config`: building description

### Door Control

- Door opening does **not** use JSON requests — it uses binary-only CTPP/CSPB channel commands
- This is why a fresh TCP connection with the 6-step binary sequence is needed

### Cloud Architecture (not used by this component)

- The official Comelit Android app routes through external servers (explains its sluggishness)
- Cloud uses MQTT (Google Cloud) + STUN/TURN (Vultr) for NAT traversal
- The `sbc.pm-always-on: false` setting means the device sleeps when idle
- This component bypasses all cloud infrastructure — direct LAN communication only

## Reference

- [ha-component-comelit-intercom](https://github.com/nicolas-fricke/ha-component-comelit-intercom) — Nicolas Fricke
- [comelit-client](https://github.com/madchicken/comelit-client) — Pierpaolo Follia (also NPM `comelit-client`)
- [Protocol analysis Part 1](https://grdw.nl/2023/01/28/my-intercom-part-1.html) — grdw (reverse engineering the ICONA protocol)
