# Comelit Local — Project Guide

## Overview

Home Assistant custom component for the **Comelit 6701W** WiFi video intercom. Communicates entirely locally via the **ICONA Bridge TCP protocol** on port 64100 — no cloud dependency.

## Project Structure

```
custom_components/comelit_intercom_local/
  __init__.py        — HA integration setup; registers both card JS static paths + Lovelace resources
  config_flow.py     — UI config flow with auto token extraction + options flow (enable_notifications)
  coordinator.py     — DataUpdateCoordinator; owns shared TCP client, RTSP server, video session, VIP listener, keepalive loop
  button.py          — Door open + Start/Stop video button entities; door button stops video after 10s delay
  camera.py          — Camera entity; is_streaming property; stop-video + state-change callbacks
  event.py           — Doorbell ring / missed call event entities
  protocol.py        — Wire protocol: 8-byte header, message types, binary payloads
  channels.py        — Channel definitions (UAUT, UCFG, CTPP, PUSH)
  client.py          — AsyncIO TCP client for ICONA Bridge; TCP keepalives; 120s read timeout
  auth.py            — Authentication flow (UAUT channel)
  token.py           — Token extraction from device HTTP backup endpoint
  config_reader.py   — Device configuration retrieval (UCFG channel)
  ctpp.py            — Shared CTPP init/handshake sequence (ctpp_init_sequence); used by door, video, VIP listener
  door.py            — Door open: open_door_fast (reuse open CTPP) + open_door_standalone (transient channel)
  push.py            — Push notification listener (PUSH channel); send_push_keepalive
  vip_listener.py    — Persistent VIP event listener on CTPP channel: doorbell_ring, door_opened, renewal ACK
  camera_utils.py    — Camera/RTSP URL discovery
  video_call.py      — Video call signaling on shared client; owns/borrows CTPP; async_open_door_on_ctpp
  rtp_receiver.py    — UDP/TCP RTP receiver: H.264 FU-A→PyAV→JPEG + PCMA audio routing; IDR logging
  rtsp_server.py     — Local RTSP server: H.264; RTCP Sender Reports; PLAY gating; disconnect_clients
  models.py          — Data models (Door, Camera, DeviceConfig, PushEvent)
  exceptions.py      — Custom exceptions
  const.py           — Constants (domain, platforms, defaults)
  www/
    comelit-intercom-card.js   — Custom Lovelace card (play-button UI, auto-stop on navigation)
    comelit-doorbell-card.js   — Doorbell notification card (ring alert, Answer/Dismiss, live stream)

tests/
  test_protocol.py        — Unit tests for wire protocol
  test_client.py          — Unit tests for TCP client
  test_ctpp.py            — Unit tests for ctpp_init_sequence
  test_door.py            — Unit tests for open_door_fast / open_door_standalone
  test_rtp_receiver.py    — Unit tests for RTP receiver
  test_rtsp_server.py     — Unit tests for RTSP server
  test_video_call.py      — Unit tests for video call session
  test_video_signaling.py — Unit tests for video signaling protocol
  test_camera.py          — Unit tests for camera entity
  test_coordinator.py     — Unit tests for coordinator
  test_vip_listener.py    — Unit tests for VIP event listener (39 tests)
  test_event_entity.py    — Unit tests for doorbell event entity (14 tests)
  test_button.py          — Unit tests for button entities
  test_push.py            — Unit tests for push channel
  test_integration.py     — Integration tests (requires real device)
  conftest.py             — Shared fixtures

postman/             — Postman collection documenting HTTP + TCP requests
```

## Setup & Development

**Requirements:** Python 3.11+, Home Assistant 2024.1+ (for HA integration)

**Always use `uv` for Python** — never use `pip` or `python3` directly.

```bash
# Install dev dependencies
uv pip install -e ".[dev]"

# Run unit tests (no device needed)
PYTHONPATH=. uv run python -m pytest tests/test_protocol.py tests/test_client.py tests/test_ctpp.py tests/test_door.py tests/test_rtp_receiver.py tests/test_rtsp_server.py tests/test_video_call.py tests/test_video_signaling.py tests/test_camera.py tests/test_coordinator.py tests/test_vip_listener.py tests/test_event_entity.py tests/test_button.py tests/test_push.py -v

# Run integration tests (requires real device on LAN)
COMELIT_HOST=192.168.1.111 COMELIT_TOKEN=<token> uv run python -m pytest tests/test_integration.py -v -s
```

## ICONA Bridge Protocol

All communication is raw TCP on port **64100**. Every message has an 8-byte header:

```
[0x00 0x06] [body_length LE16] [request_id LE16] [0x00 0x00]
```

### Channels and Flow

1. **UAUT** — Authentication: open channel → send JSON access request with token → expect code 200
2. **UCFG** — Configuration: request config → parse doors, cameras, apt_address
3. **PUSH** — Notifications: registers FCM token; also used as keepalive probe (re-send push-info every 90s — device ACKs with JSON, resetting the idle timer)
4. **CTPP** — Persistent channel for VIP events (doorbell ring, door opened) and door control; shared across VIP listener, video session, and standalone door open (see Door Control below)
5. **UDPM/RTPC** — Video call signaling (uses `trailing_byte=1`)

### Critical Protocol Rules

- **Channel open sequence must always be 1** — device ignores packets with seq != 1
- **Timeout must be >= 30s** — device can be very slow to respond
- **Request ID** starts semi-random (8000+) and increments per message
- After channel open, server responds with `server_channel_id` used for subsequent messages
- JSON messages use compact format: `separators=(",", ":")`

## Key Entities

All entities use `_attr_has_entity_name = True`. The device name is derived from `coordinator.device_name` (the config entry title), so entity IDs reflect the user-configured name set during setup (e.g. `"Front Door"` → `button.front_door_actuator`). Default is `"Comelit <host>"` when no name is set.

| Entity | Description |
|--------|-------------|
| `button.<name>_<door_name>` | Press to open door/gate; stops video 10s after if active |
| `event.<name>_doorbell` | Fires `doorbell_ring` and `missed_call` events |
| `camera.<name>_live_feed` | Live video stream from intercom (`is_streaming` reflects session state) |
| `button.<name>_start_video_feed` | Manually trigger video call |
| `button.<name>_stop_video_feed` | Stop active video call |

### Entity ID Note

Door `id` from device can be non-unique (e.g., both doors had id=0). The `index` field on the Door model is a sequential counter used for unique entity IDs.

Entity IDs are persisted in HA's entity registry by `unique_id`. If upgrading from an older version with different IDs, delete and re-add the integration or rename manually in Settings → Entities.

## Door Control

Three code paths selected automatically by `coordinator.async_open_door`:

**Path 1 — video active** (`video_call.py` — single message on existing CTPP channel):
- PCAP-verified (`camera_feed_with_open_door_local.pcap`): the Android app sends a **single `0x1840/0x000D` message** on the video CTPP channel — no new channel, no 6-step sequence
- `VideoCallSession.async_open_door_on_ctpp(our_addr, entrance_addr, relay_index)` — increments call counter under `_ctpp_lock`, sends `encode_door_open_during_video`
- Device ACKs with `0x1800/0x0000`; relay activates immediately
- `relay_index` = `door.output_index`

**Body structure of `encode_door_open_during_video` (48 bytes):**
```
[LE16 0x1840] [LE32 counter] [BE16 0x000D] [BE16 0x002D]
[entrance_addr padded to 10 bytes] [LE32 relay_index] [4× 0xFF]
[our_addr padded to 10 bytes] [entrance_addr padded to 10 bytes]
```

**Path 2 — VIP listener active, no video** (`door.py` → `open_door_fast`):
- Reuses the already-open CTPP channel; skips the init handshake entirely
- Fires `encode_open_door` + `encode_open_door_confirm` twice (~30 ms total)
- Used when notifications are enabled and no video is running

**Path 3 — no CTPP channel open** (`door.py` → `open_door_standalone`):
- Opens a transient `CTPP_DOOR` channel with full `ctpp_init_sequence`
- 6-step sequence: init → read 2 responses → ACK pair → open+confirm × 2 → close channel
- Used when notifications are disabled

**`ctpp_init_sequence` (shared via `ctpp.py`):**
1. Send `encode_ctpp_init` (apt_addr, apt_sub, timestamp)
2. Drain up to 2 responses (optional, device may not reply)
3. Send ACK pair: `encode_call_response_ack` with prefix `0x1800` then `0x1820`, timestamp `= init_ts + 0x01010000`

## Video Streaming

- `video_call.py` handles TCP signaling on the **shared coordinator client**: reuses open CTPP when VIP listener is active (skips init), opens its own if not (`_owns_ctpp = True`)
- `rtp_receiver.py` handles UDP reception: ICONA header → RTP → H.264 FU-A → PyAV decode → JPEG; NAL queue carries `(rtp_ts, nal_bytes)` tuples; logs IDR keyframe intervals for freeze diagnosis
- `rtsp_server.py` serves H.264 over local RTSP (TCP interleaved); monotonic timestamps rebased across calls
- Video config sends resolution 800×480 at 25 FPS
- Video does **not** auto-start on doorbell ring — user controls via button, Lovelace card, or automation
- VIP listener is paused (`stop_task()`) before video starts so the session can own the CTPP channel; restarted in `async_stop_video` via `_ensure_vip_listener()`
- **Persistent RTSP server** owned by coordinator — started at HA setup, never stopped between calls; `stream_source()` always returns a valid URL
- **`_video_ready_event`** (asyncio.Event) gates both `stream_source()` and the RTSP `PLAY` handler — clients stall inside PLAY during the CTPP handshake instead of erroring on an empty stream and triggering a 10s HA backoff
- **`_video_start_lock`** (asyncio.Lock) in coordinator prevents concurrent `async_start_video` calls
- **`disconnect_clients()`** on new session start — forces go2rtc to re-`DESCRIBE` against a stream with video already flowing (avoids 20+ s delay for go2rtc to detect a new video track on an existing connection)
- **RTCP Sender Reports** — periodic (5s) SR packets with NTP/RTP timestamp pairs; fixes "no reference clock" delays in VLC, go2rtc and browsers
- **`is_streaming` property** on camera entity — reflects active session state so HA frontend correctly shows "streaming" vs "idle" and go2rtc attaches via WebRTC on the first session
- **Inline re-establishment** on CALL_END (~30s): ACK → refresh RTPC_LINK → VIDEO_CONFIG_RESP — no TCP reconnect, video is uninterrupted
- Video falls back to TCP transport (RTPC2) if UDP is blocked by NAT/firewall

## Audio Streaming

- Audio does **NOT** start automatically — device requires an explicit "answer" sequence after video starts
- **Answer sequence** (sent as background task after video is flowing, non-fatal):
  1. `encode_answer_video_reconfig` — prefix `0x183C`, resends 800×480 @ 25fps
  2. `encode_answer_peer` — prefix `0x1830` (or `0x1860` for renewal), action `0x70`, signature: `(caller, entrance_addr, timestamp, renewal=False)`
  3. `encode_answer_config_ack` — prefix `0x180C`, action `0x000E`
- Device responds by opening a new RTPC channel; audio flows ~3.5s later
- **Audio codec: PCMA G.711 A-law, PT=8, 20ms frames (160 bytes/frame)**
- Audio arrives on same UDP port as video, distinguished by RTP payload type (PT=8)
- Silent PCMA keepalive is **disabled** — it was ticking the 8 kHz audio clock ~50× too slowly, causing HLS/WebRTC stutters
- **Hangup:** `encode_hangup` in `protocol.py`, action `0x2d` + entrance address
- See `docs/audio_protocol_findings_2026_03_22.md` for protocol analysis
- See `docs/implementation_state_2026_03_25.md` for full implementation notes

## Testing Device

- HTTP port: `8080`, ICONA port: `64100`
- Credentials: `admin` / `comelit`, token in `.env` (COMELIT_TOKEN)
- Config: apt_address=SB000006, apt_subaddress=1, 2 doors (Actuator, Entrance Lock), 0 cameras

## Lovelace Cards

Both cards are automatically registered on HA startup via `StaticPathConfig` (HA 2024.7+) and versioned Lovelace resource URLs.

**Intercom camera card** (`www/comelit-intercom-card.js`):
- Shows camera snapshot with play button overlay; click to start video
- Live view uses `hui-picture-entity-card` (created via `window.loadCardHelpers()` to ensure element is upgraded before `setConfig`)
- Stops video on navigation away (`location-changed` + `getBoundingClientRect()`) or DOM removal
- Card config:
  ```yaml
  type: custom:comelit-intercom-card
  camera_entity: camera.comelit_intercom_live_feed
  start_entity: button.comelit_intercom_start_video_feed  # optional
  stop_entity: button.comelit_intercom_stop_video_feed
  ```

**Doorbell notification card** (`www/comelit-doorbell-card.js`):
- States: Idle (thumbnail + doorbell badge) → Ringing (pulsing icon + Answer/Dismiss) → Answered (live stream + stop button)
- Auto-dismisses after `dismiss_after` seconds (default 30)
- Card config:
  ```yaml
  type: custom:comelit-doorbell-card
  doorbell_entity: event.comelit_intercom_doorbell
  camera_entity: camera.comelit_intercom_live_feed
  start_entity: button.comelit_intercom_start_video_feed
  stop_entity: button.comelit_intercom_stop_video_feed
  dismiss_after: 30  # optional
  ```

## HA Debug Logging

```yaml
logger:
  default: info
  logs:
    custom_components.comelit_intercom_local: debug
```

## Workflow Preferences

- **Use expert agents (subagents) whenever possible** — delegate research, code exploration, and independent subtasks to subagents to parallelize work and keep the main context clean.

## Protected Files

- **`custom_components/comelit_intercom_local/door.py` is locked** — do NOT edit this file unless the user explicitly says so. It reached a stable, verified state after a careful refactoring and bug-fix session. Any unintended change risks re-introducing protocol bugs that break door opens on the real device.

- **`custom_components/comelit_intercom_local/video_call.py` is locked** — do NOT edit this file unless the user explicitly says so. The video signaling flow (start, inline renewal, CTPP monitor loop, RTPC ACK logic) reached a stable, verified state after an extensive PCAP-driven bug-fix session. Any unintended change risks breaking video start, renewal, or door-open-during-video on the real device.

## Flow Protection Rule

The **video feed flow** and the **door opening flow** are the two verified, working flows — they are the highest priority and must never be broken.

- The two locked files above (`door.py`, `video_call.py`) must not be edited without explicit permission.
- For **any other shared file** (`client.py`, `coordinator.py`, `protocol.py`, `ctpp.py`, `channels.py`, `rtp_receiver.py`, `rtsp_server.py`, etc.): if a proposed change touches code paths used by either flow, **stop and ask the user before making the change**. Describe what would change and its potential impact, then wait for approval.
- This applies even when the change is for an unrelated feature. When in doubt, ask.

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

### VIP Event Listener

- The PUSH channel only registers an FCM token — actual call events (doorbell ring, door opened) arrive as binary messages on the persistent CTPP channel
- `VipEventListener` opens `CTPP_VIP` + `CSPB_VIP` at startup, runs `ctpp_init_sequence`, and listens in a background task
- The device sends a periodic registration renewal signal (`0x1860/0x0010`); the listener must ACK with `0x1800` + `0x1820` or the device stops sending events
- Action codes: `0x18C0` (call init) and `0x1860/0x0001` (IN_ALERTING) → `doorbell_ring`; `0x1860/0x0003` → `door_opened`
- Events are deduplicated within a 10s window to suppress device retransmissions
- VIP listener is paused during video and restarted after `async_stop_video`

### Door Control

- Door opening does **not** use JSON requests — it uses binary-only CTPP/CSPB channel commands
- Three paths: video active (single message on CTPP), VIP listener open (fast path, reuse CTPP), no CTPP (standalone with full init)
- See Door Control section above for full details

### Cloud Architecture (not used by this component)

- The official Comelit Android app routes through external servers (explains its sluggishness)
- Cloud uses MQTT (Google Cloud) + STUN/TURN (Vultr) for NAT traversal
- The `sbc.pm-always-on: false` setting means the device sleeps when idle
- This component bypasses all cloud infrastructure — direct LAN communication only

## Reference

- [ha-component-comelit-intercom](https://github.com/nicolas-fricke/ha-component-comelit-intercom) — Nicolas Fricke
- [comelit-client](https://github.com/madchicken/comelit-client) — Pierpaolo Follia (also NPM `comelit-client`)
- [Protocol analysis Part 1](https://grdw.nl/2023/01/28/my-intercom-part-1.html) — grdw (reverse engineering the ICONA protocol)
