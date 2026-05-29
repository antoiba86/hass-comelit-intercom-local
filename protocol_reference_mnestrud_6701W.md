# ICONA Bridge Protocol Reference — Comelit 6701W

**Author**: Michael Nestrud ([@mnestrud](https://github.com/mnestrud))  
**Device**: Comelit 6701W WiFi video intercom (indoor unit, model `MSVU`, firmware 2.1.0)  
**Source**: PCAP analysis of official Comelit Android app (two separate captures)  
**Protocol port**: TCP 64100  
**Last updated**: 2026-05-29

This document captures the full ICONA Bridge binary/JSON protocol as observed on the Comelit 6701W. It extends the earlier reverse engineering by [grdw](https://grdw.nl/2023/01/28/my-intercom-part-1.html) and [madchicken/comelit-client](https://github.com/madchicken/comelit-client) with verified wire-level details for inbound call answering, media transport, and the CTPP binary protocol.

**PCAPs analysed**:
- PCAP1 — initial inbound call capture (ring only, no answer from phone)
- PCAP2 — second capture with full phone answer sequence

**Device info** (read from `server-info` JSON response):
- Model: `MSVU`
- Firmware: `2.1.0`
- Serial: `<serial>` (device-specific)
- Apartment address: `<apt_addr>`, subaddress `<apt_sub>` (giving full addr `<apt_addr_sub>`)
- Entrance/outdoor unit address: `<entrance_addr>` (from ring PCAP — caller field of `0x18C0/0x0028`)
- Door count: 2 (names are user-configurable)
- Camera count: 0 (camera is built into outdoor unit, accessed via video call)

---

## 1. ICONA Message Frame

Every message — JSON or binary — is wrapped in an 8-byte header:

```
[0x00][0x06]  magic (always these 2 bytes)
[lo][hi]      body_length  (LE uint16, length of body only, not including header)
[lo][hi]      request_id   (LE uint16, channel ID once open; 0 for channel management)
[0x00][0x00]  padding
<body bytes>
```

Multiple ICONA messages may be concatenated in a single TCP segment. The reader must frame them using `body_length`.

---

## 2. Channel Management Messages

### 2a. Channel OPEN (client → device)

```
MessageType.COMMAND (0xABCD, LE16)
sequence (LE16, always 1)
channel_type_id (LE32, always 7 = 0x00000007)
channel_name (4 ASCII bytes, no null)
request_id (LE16, the channel's future req_id)
trailing_byte (1 byte, 0 for most channels; 1 for RTPC/UDPM)
[optional extra_data block]:
  0x00                          pad byte (PCAP-verified: must be present before extra_len)
  len+1 (LE32)                  length of extra string including null terminator
  extra_string + 0x00           null-terminated ASCII string
```

### 2b. Channel OPEN ACK (device → client)

```
MessageType.COMMAND (0xABCD, LE16)
0x0002 (LE16)      response type
0x00000004 (LE32)  constant
request_id (LE16)  echoed back
0x0000 (LE16)      padding
[optional extra bytes — e.g. UDPM returns 0x00000002 + media_server_channel_id LE16]
```

### 2c. Device-initiated Channel OPEN (device → client)

When the device opens a channel (e.g., RTPC during an inbound answer), the ICONA header has `request_id = 0`. The ABCD body is slightly different from the client-initiated format:

**PCAP2-verified body** (15 bytes, no null terminator after channel name):
```
cd ab 01 00 07 00 00 00 52 54 50 43 01 fd 01
│         │            │           │  │  └─ trailing_byte
│         │            │           │  └─── server_channel_id lo (0xFD)
│         │            │           └────── server_channel_id hi (0x01) → 0x01FD = 509 decimal
│         │            └────────────────── "RTPC" (4 bytes, NO null terminator)
│         └─────────────────────────────── channel_type_id=7 (LE32)
└───────────────────────────────────────── 0xABCD seq=1 (LE16+LE16)
```

The `server_channel_id` is at `body[-3:-1]` (LE16) = bytes 12–13, NOT at the usual offset 8. Parse using `len(body)-3` as the offset for bodies where the channel name is not null-terminated.

**Client must respond** with a COMMAND at seq=2, `channel_type_id=4`, echoing the device's `request_id`:
```
cd ab 02 00 04 00 00 00 [request_id lo][request_id hi] 00 00
```

### 2d. Channel END (client or device)

```
MessageType.END (0x01EF, LE16)
0x0003 or 0x0004   sub-type
0x00000002         constant
request_id (LE16)  channel being closed
[optional 0x0000 padding]
```

---

## 3. Channel Types and Purposes

| Name | trailing_byte | extra_data | Purpose |
|------|--------------|------------|---------|
| UAUT | 0 | — | Authentication: send JSON `access` with `user-token`; expect code 200 |
| UCFG | 0 | — | Configuration: `get-configuration` returns doors, cameras, apt_address |
| INFO | 0 | — | Server info: `server-info` returns model, version, capabilities |
| CTPP | 0 | apt_addr+sub (e.g. `<apt_addr_sub>`) | Persistent VIP events: doorbell ring, door opened, keepalive |
| CSPB | 0 | — | Opened after CTPP; no messages sent on it (role unknown) |
| PUSH | 0 | — | FCM token registration; keepalive probe every 90s |
| FRCG | 0 | — | Face recognition: `rcg-detected-recognition` and `rcg-detected-image` notifications |
| RTPC | 1 | — | Video call signaling (first RTPC); device-opened variant carries H.264/PCMA media |
| RTPC (2nd) | 1 | — | Second RTPC channel opened during inbound answer (wire name "RTPC") |
| UDPM | 1 | — | Media: open ACK returns `media_server_channel_id` in extra bytes |

**Channel open order (Android app, full PCAP-verified sequence)**:  
UAUT → UCFG → INFO (`server-info`) → CTPP + CSPB + ctpp_init → close INFO → UCFG second `get-configuration` → FRCG (`rcg-get-params`) → UAUT2 `access` (no device response on firmware 2.1.0) → UCFG2 open (stays open, no messages) → PUSH

**Note on UAUT2**: The Android app opens a second UAUT channel and sends an `access` request after FRCG. On the 6701W (firmware 2.1.0) the device does **not** respond to this second authentication. Adding a response wait times out. Skip UAUT2 entirely.

---

## 4. JSON Channel Protocol

Used by UAUT, UCFG, INFO, PUSH, FRCG. All JSON uses compact separators `(",", ":")`.

### Authentication (UAUT)
```json
{"message":"access","user-token":"<token>","message-type":"request","message-id":13}
{"message":"access","message-type":"response","message-id":13,"response-code":200,"response-string":"Access Granted"}
```

### Configuration (UCFG)
```json
{"message":"get-configuration","address-books":"all","message-type":"request","message-id":14}
```
Response contains `vip.apt-address`, `vip.apt-subaddress`, entrance/actuator/opendoor address books.

### Push (PUSH)
```json
{"message":"push-info","message-type":"request","message-id":1,"os-type":"android","device-token":"<fcm_token>","bundle-id":"com.comelit.bigapp","profile-id":"1","apt-address":"<apt_addr>","apt-subaddress":<apt_sub_int>}
```

### Face Recognition (FRCG)
```json
{"message":"rcg-get-params","message-type":"request","message-id":121}
```
Device sends unsolicited notifications:
```json
{"message":"rcg-detected-recognition","message-type":"notification","message-id":1,"notification-code":200,"notification-string":"Recognition Detected","data":{"session-id":"<session_id>","external-id":"unknown","recognized":false,"activated":false,"similarity":"3.079382","confidence":"1.18762e-308","image-url":"https://eps.cloud.comelitgroup.com/servicerest/facerecognition/getfaceimagebykey/v1/<key>.jpeg","box":{"width":"0.172588","height":"0.318547","top":"0.0898776","left":"0.00707681"}}}
{"message":"rcg-detected-image","message-type":"notification","message-id":2,"notification-code":200,"notification-string":"Face Detected","image":{"path":"/etc/comelit/recognition/detected/unknown/<timestamp>.jpg","external-id":"unknown","changed":true}}
```

**Note**: The `image-url` field points to Comelit's cloud infrastructure (`eps.cloud.comelitgroup.com`). The `path` field is a local filesystem path on the device itself (accessible only if you have shell access). Local integration implementations can use `path` for fully offline face image retrieval.

---

## 5. CTPP Binary Message Format

CTPP carries binary VIP events directly (not JSON). **Mixed endian** — prefix and timestamp are little-endian; action and flags are big-endian.

```
prefix    (LE uint16)  — 0x18C0, 0x1800, 0x1820, 0x1840, 0x1860
timestamp (LE uint32)  — counter/session seed
action    (BE uint16)  — event type
[flags    (BE uint16)  — present on 0x1840/0x1860/0x18C0; absent on 0x1800/0x1820]
[extra bytes]          — message-type-specific payload
0xFFFFFFFF             — wildcard separator (present on 0x1840/0x18C0; absent on 0x1800/0x1820)
caller\0               — null-terminated ASCII address string
callee\0\0             — null-terminated ASCII address string (2 nulls = wider pad)
```

Address format: `SBnnnnnnns` where `nnnnnn` is 6-digit address, `s` is subaddress digit. Padded to 10 bytes total.

### Prefix meanings
| Prefix | Direction | Meaning |
|--------|-----------|---------|
| 0x18C0 | device→client | Call init / doorbell ring (5 retransmits, ~7.5s) |
| 0x18C0 | client→device | CTPP init/subscription message (action 0x0011) |
| 0x1800 | both | ACK (no flags field; no 0xFFFFFFFF separator) |
| 0x1820 | both | Confirm-ACK (same format as 0x1800) |
| 0x1840 | both | Call-phase event / codec negotiation |
| 0x1860 | device→client | VIP FSM event |

### Known actions
| Action | Prefix | Direction | Meaning |
|--------|--------|-----------|---------|
| 0x0000 | 0x1800/0x1820 | both | ACK / keepalive |
| 0x0002 | 0x1840 | client→device | Call accepted (inbound answer only) |
| 0x0003 | 0x1840 | client→device | RTPC2-ready (inbound answer, flags=0x000A) |
| 0x0008 | 0x1840 | both | Codec negotiation |
| 0x000A | 0x1840 | client→device | RTPC link (contains RTPC1 channel ID in extra) |
| 0x000D | 0x1840 | client→device | Door open (during active video call) |
| 0x0011 | 0x18C0 | client→device | CTPP subscription / init (flags=0x0040) |
| 0x001A | 0x1840 | client→device | Video config (resolution, RTPC2 ch_id, media_req_id) |
| 0x0028 | 0x18C0 | device→client | Doorbell ring |
| 0x0000 | 0x1860 | device→client | Device returned to idle |
| 0x0001 | 0x1860 | device→client | IN_ALERTING (alternate ring signal; not seen on 6701W) |
| 0x0002 | 0x1860 | device→client | Call connected (outbound) |
| 0x0003 | 0x1860 | device→client | Door opened |
| 0x0004 | 0x1860 | device→client | OUT_ALERTING (outbound ring) |
| 0x0005 | 0x1860 | device→client | Call closed |
| 0x000A | 0x1860 | device→client | Call terminated by far end |
| 0x0010 | 0x1860 | device→client | Registration renewal (must ACK with 0x1800+0x1820 pair) |

---

## 6. CTPP Init / VIP Subscription Message

Sent once after opening the CTPP channel.

```
0x18C0           (LE16)
timestamp        (LE32) — int(time.time()) & 0xFFFFFFFF
0x0011           (BE16) action
0x0040           (BE16) flags
0x18 0xC2        (2 bytes) capability constant — PCAP-verified hardcoded value, NOT timestamp-derived.
                           Device echoes the same bytes back in 0x1860/0x0010 renewal responses.
                           Code that sends (ts & 0xFFFF) here is WRONG.
apt_addr+sub\0           e.g. "<apt_addr_sub>\0"
0x10 0x0E                separator
0x00 0x00 0x00 0x00      zero pad
0xFF 0xFF 0xFF 0xFF      wildcard
apt_addr+sub\0           e.g. "<apt_addr_sub>\0" (repeated)
apt_addr\0\0             e.g. "<apt_addr>\0\0"
```

Device responds with:
1. `0x1800/0x0000` ACK
2. `0x1860/0x0010` registration renewal (must ACK with `0x1800 + 0x1820` pair, ts = init_ts + 0x01010000)

---

## 7. Outbound Call Sequence (client-initiated)

Key points:
- `encode_call_init()` sends `0x18C0/0x0028` with codec marker `II` (0x49 0x49)
- Counter seed derived from response timestamp
- `codec_param = 0x27` in `encode_call_ack`
- Device opens RTPC channel (device-initiated ABCD body; see Section 2c)
- Call lasts ~30s then device sends CALL_END; inline renewal keeps it alive

---

## 8. Inbound Answer Sequence (PCAP2-verified)

**Ring ACK acceptance indicator**: If ring ACK is accepted → zero retransmits and device sends codec bundle immediately (~0.28s). If rejected → device retransmits ring 4–5 times over ~7.5s then times out silently.

**Critical differences from outbound**:
- `fresh_ts` is computed from `ring_ts` via a bit-manipulation transform (Section 8a)
- `codec_param = 0x07` (not 0x27)
- All CTPP messages use `callee = own_base_addr` (e.g. `<apt_addr>`), NOT entrance_addr
- No `encode_call_init` (0x18C0) sent — device already initiated the call
- Two extra messages: `encode_rtpc2_ready` (0x1840/0x0003) and `encode_call_accepted` (0x1840/0x0002)

---

### 8a. fresh_ts Computation (PCAP2-verified)

`fresh_ts` is NOT `int(time.time())`. It is derived from the ring's `ring_ts` field via a byte-level transform:

```python
_rb = bytearray(struct.pack("<I", ring_ts))
_rb[0] |= 0x80
_rb[2], _rb[3] = _rb[3], (_rb[2] + 1) & 0xFF
fresh_ts = struct.unpack("<I", bytes(_rb))[0]
```

**PCAP2 verification**:
- ring_ts = `0xF5382558` → bytes LE: `[58, 25, 38, F5]`
- Step 1: `[58, 25, 38, F5]` → byte[0] |= 0x80 → `[D8, 25, 38, F5]`
- Step 2: swap(byte[2], byte[3]+1): byte[2]=F5, byte[3]=(38+1)=39 → `[D8, 25, F5, 39]`
- fresh_ts = `0x39F525D8` ✓ (confirmed against PCAP2 ACK1 timestamp)

**The same transform is applied when ACKing any device-sent 0x1840 CTPP message** (rtpc_link, PEER, etc.). The device's ts field is used as the input, and the transform output is the ts in our ACK. This is a general "device ts → client ACK ts" formula, not just for the ring.

**PCAP2 rtpc_link verification**:
- device rtpc_link ts = `0xABC8B83B` → bytes: `[3B, B8, C8, AB]`
- Step 1: `[BB, B8, C8, AB]` (byte[0] |= 0x80)
- Step 2: byte[2]=AB, byte[3]=(C8+1)=C9 → `[BB, B8, AB, C9]`
- ACK ts = `0xC9ABB8BB` ✓

---

### 8b. Counter Model (all 8 positions PCAP2-verified)

```
B4 = 0x00010000
B5 = 0x01000000
call_counter = (fresh_ts + B5) & 0xFFFFFFFF   ← this is the primary working counter

Position          ts value                  Message
────────────────────────────────────────────────────────────────────────
fresh_ts          step 1: 0x1800 ACK1
                  step 5: encode_call_ack (codec ACK, initial)
call_counter      step 6: encode_call_ack (codec ACK retransmit) ← PCAP2-verified; NOT fresh_ts
                  step 9: 0x1800 ACK2 (after bundle)
call_counter+B4   step 11: encode_rtpc2_ready
call_counter+2×B4 step 13: encode_rtpc_link
call_counter+3×B4 step 14: encode_video_config (initial)
call_counter+4×B4 step 16: encode_video_config (retransmit, ~3s later)
call_counter+5×B4 step 17: encode_answer_peer (inbound=True)
call_counter+6×B4 step 18: encode_call_accepted
```

**Note**: PCAP1 analysis incorrectly showed codec retransmit uses `fresh_ts`. PCAP2 confirms it uses `call_counter` (= fresh_ts+B5).

---

### 8c. Full Step-by-Step Sequence

All timings relative to ring_ts receipt. PCAP2 timings shown where available.

```
Pre-ring: maintain CTPP session (ctpp_init already sent, keepalives running)

On ring (0x18C0/0x0028 received):
  ring_ts = struct.unpack_from("<I", body, 2)[0]
  entrance_addr = caller field from ring body
  fresh_ts = transform(ring_ts)                (see Section 8a)
  call_counter = (fresh_ts + B5) & 0xFFFFFFFF
  our_addr = apt_addr + str(apt_subaddress)    (e.g. "<apt_addr_sub>")
  our_base_addr = apt_addr                     (e.g. "<apt_addr>")

Step 1  [+0.00s]  Send 0x1800 ACK1
                    ts=fresh_ts, caller=our_addr, callee=our_base_addr
                    Format: [0x1800][fresh_ts][0x0000][0xFFFFFFFF][our_addr\0][our_base_addr\0\0]

Step 2  [+0.001s] Open RTPC (trailing_byte=1) — do not wait for ACK yet

Step 3  [+0.001s] Open UDPM (trailing_byte=1) — extract media_server_channel_id from ACK extra

Step 4  [+0.001s] Send encode_call_ack(our_addr, our_base_addr, fresh_ts, codec_param=0x07)
                    Action 0x1840/0x0008, flags=0x0003
                    extra = [49 00 07 00 00 00]  ('I', 0x00, 0x07, ...)

Step 5  [+0.001s] Wait for RTPC and UDPM channel ACKs
                    RTPC ACK → rtpc1_req_id = media_req_id (from channel open response)
                    UDPM ACK → udpm_token from extra bytes

Step 6  [+0.001s] Setup RTP receiver (UDP receive on UDPM port)

       [~0.18s]   Wait for device bundle (first non-ring 0x1840/0x0008 response)
                    Device sends codec bundle: 0x1840/0x0008 (codec response)
                    If device retransmits ring here → ring ACK was rejected (fatal)

Step 7  [after bundle] Send 0x1800 ACK2
                    ts=call_counter (=fresh_ts+B5), caller=our_addr, callee=our_base_addr

       [~PCAP2 +0.372s] Simultaneously open RTPC2 (trailing_byte=1, wire_name="RTPC")

Step 8  [+0.001s after ACK2] Send codec ACK retransmit
                    encode_call_ack(our_addr, our_base_addr, call_counter, codec_param=0x07)
                    ts=call_counter (NOT fresh_ts — PCAP2-verified)

Step 9  [await RTPC2 open] Capture rtpc2.media_req_id

Step 10 [+0.49s]  Send encode_rtpc2_ready(our_addr, our_base_addr, call_counter+B4)
                    Action 0x1840/0x0003, flags=0x000A, extra=b"\x00\x00"
                    Body = 36 bytes (PCAP2-verified)

Step 11 [+0.49s]  Send encode_rtpc_link(our_addr, our_base_addr, rtpc1_req_id, call_counter+2×B4)
                    Action 0x1840/0x000A, flags=0x0011
                    extra = [18 02 00 00 00 00][rtpc1_req_id LE16][00 00]

Step 12 [+0.49s]  Send encode_video_config(our_addr, our_base_addr, rtpc2_req_id, call_counter+3×B4,
                                          width=320, height=240)
                    Action 0x1840/0x001A, flags=0x0011
                    extra = [14 32 00 00 00 00][rtpc2_req_id LE16][FF FF C0 00 00 00]
                             [40 01 F0 00][40 01 F0 00][10 00][00 00]
                    = 320×240 primary, 320×240 secondary, 16fps
                    NOTE: extra[10:14] = C0 00 00 00; inbound primary resolution = 320×240 (NOT 800×480)

Step 13 [+3.49s]  Retransmit encode_video_config with ts=call_counter+4×B4, same args

Step 14 [+3.89s]  Send encode_answer_peer(caller=our_addr, callee=our_base_addr,
                                          timestamp=call_counter+5×B4, inbound=True)
                    48 bytes — see Section 8d for exact wire format
                    callee = our_base_addr (NOT entrance_addr); caller IS present after separator

Step 15 [+3.89s]  Send encode_call_accepted(our_addr, our_base_addr, call_counter+6×B4)
                    Action 0x1840/0x0002, flags=0x000C
                    extra = b"\x00\x00"   (4 total FF bytes = separator only)
                    See Section 8e for exact wire format

Step 16 [post call_accepted] Drain device signaling:
                    Device sends 0x1840/0x000A (rtpc_link) and 0x1840/0x000E (PEER)
                    Client must ACK each with 0x1800 using transform(device_ts) (Section 8a)
                    Wait for device_rtpc.open_event (device-initiated RTPC channel open)

Step 17 [on open_event] Capture device_rtpc.server_channel_id
                    Connect media pipeline (see Section 9)
```

---

### 8d. encode_answer_peer — Inbound Wire Format (live-verified, 48 bytes)

```
[prefix LE16]         0x1840
[timestamp LE32]      call_counter+5×B4
[inner_len BE16]      2 + len(inner_payload)   where inner_payload = caller\0 + flag
[0x0070 BE16]         ACTION_PEER
[caller\0]            e.g. "<apt_addr_sub>\0"
[0x01 0x00]           flag (inbound initial)
[0x00 0x00]           extra padding (inbound only; absent in outbound format)
[0xFF 0xFF 0xFF 0xFF] separator
[caller\0]            e.g. "<apt_addr_sub>\0"   ← caller IS repeated after separator
[our_base_addr\0\0]   e.g. "<apt_addr>\0\0"     ← callee = our_base_addr (NOT entrance_addr)
```

**Comparison — outbound format** (46 bytes):
```
... [caller\0][0x01 0x00][0xFF 0xFF 0xFF 0xFF][caller\0][entrance_addr\0\0]
```
Outbound: callee after separator = entrance_addr; no `0x0000` padding before separator.

---

### 8e. encode_call_accepted — Wire Format (live-verified, 36 bytes)

```
[0x1840 LE16]
[timestamp LE32]      call_counter+6×B4
[0x0002 BE16]         action = ACTION_CALL_ACCEPTED
[0x000C BE16]         flags
[0x00 0x00]           extra = b"\x00\x00" (no extra FF bytes)
[0xFF 0xFF 0xFF 0xFF] separator (from _build_ctpp_video_msg)
[caller\0]            e.g. "<apt_addr_sub>\0"
[callee\0\0]          e.g. "<apt_addr>\0\0"
```
Total FF bytes in body = **4** (separator only). `extra=b"\x00\x00"` in code. Earlier analysis incorrectly thought extra contained `\xff\xff` (6 total FF).

---

### 8f. Codec Negotiation Difference

| Direction | extra bytes (hex) | Notes |
|-----------|-------------------|-------|
| Outbound (client→device initiates) | `49 00 27 00 00 00` | 'I', 0x00, 0x27 |
| Inbound (device→client initiates)  | `49 00 07 00 00 00` | 'I', 0x00, 0x07 |

Device responds to inbound codec ACK with: `50 00 33 b0 00 00` ('P', 0x00, 0x33, ...). Note 'P' not 'I'.

---

### 8h. Device ACK Counter (live-test confirmed)

After receiving our 0x1840 answer sequence messages, the device sends one `0x1800/0x0000` ACK per 0x1840 received. The device's ACK timestamps use its own counter seeded from `ring_ts`:

```
device_ack_base = ring_ts + 0x01010000   (ring_ts + B4 + B5)
device_ack[0]   = device_ack_base
device_ack[1]   = device_ack_base + B4
device_ack[2]   = device_ack_base + B4 + B5
... (continues incrementing by B4 or B5)
```

These 0x1800 ACKs from the device do NOT require a response from us.

---

## 9. RTP Media Stream

### Inbound call media transport (live-verified)

**Device sends media via TCP** on the channels we opened — NOT via UDP.

| Channel | RTP PT | Content |
|---------|--------|---------|
| RTPC1 (client-opened) | 8 (PCMA) | Audio from outdoor unit |
| RTPC2 (client-opened) | 99 (H.264) | Video from outdoor unit |

Client strips 8-byte ICONA header before queuing data → `channel.response_queue` contains raw RTP.  
Route via `receiver.receive_tcp_rtp(data)` — drain both queues continuously in a background loop.

**Zero UDP media from device during inbound calls** — confirmed by pcap (only small keepalive responses from device over UDP).

### Outbound call media transport
- Device sends media via **UDP** to port 64100 with ICONA `request_id` = RTPC2 `server_channel_id`
- Handle in `_on_udp_packet`

### Video codec
- H.264, RTP payload type 99
- FU-A fragmented NAL units (must be reassembled before decode)
- 320×240 resolution for inbound; 800×480 for outbound

### Audio codec
- PCMA G.711 A-law, PT=8, 20ms frames (160 bytes/frame), 8 kHz

### Outbound audio (client → device)
- Sent via UDP with ICONA `request_id` = `device_rtpc.server_channel_id` (device-opened RTPC)
- PT=8 PCMA, silence = 0xD5 bytes (G.711 A-law encoding of 0)

---

## 10. VIP Channel Keepalive

The device sends `0x1860/0x0010` (ACTION_REGISTRATION_RENEWAL) periodically (~every 20s).  
Client must respond with `0x1800 + 0x1820` pair using `ack_ts = init_ts + 0x01010000`.  
If not ACKed, the device stops sending VIP events (rings, door opens).

---

## 11. Device-Specific Notes (6701W firmware 2.1.0)

- Disconnects from WiFi when idle — physically wake before any network test
- Only accepts **one CTPP session** at a time — opening a second CTPP from a different client while one is active causes the device to silently ignore all messages from the second client
- Sends exactly 5 ring retransmits over ~7.5s (intervals roughly doubling: 0, 0.6, 1.2, 3.4, 7.5s)
- Does **not** respond to a second UAUT `access` request on firmware 2.1.0 — skip UAUT2
- **Model**: MSVU (read from `server-info` JSON response)
- **Firmware**: 2.1.0 (read from `server-info`)
- Face recognition pipeline runs locally; captured images stored at `/etc/comelit/recognition/detected/`
- Cloud endpoints used by stock app: `hub-vc-generic.cloud.comelitgroup.com:443` (MQTT), `eps.cloud.comelitgroup.com` (face recognition images)

---

## 12. PCAP1 Findings (initial capture — ring only, no answer)

### Confirmed correct
- Ring ACK (`0x1800/0x0000`) body format — byte-identical to PCAP
- RTPC channel open format (15-byte body, `channel_type_id=7`, `trailing_byte=1`)
- UDPM channel open format (same as RTPC)
- Codec ACK (`0x1840/0x0008`) body format, codec_param=0x07 for inbound
- Counter model (8 positions)

### Found wrong
- **ctpp_init capability bytes** (body bytes [10:12]): must be `18 c2` (fixed protocol constant), NOT `struct.pack("<H", ts & 0xFFFF)`. The device echoes these bytes back in `0x1860/0x0010` renewal responses, which is how this was confirmed.

---

## 13. PCAP2 Findings (full phone-answer capture)

PCAP2 includes a complete phone answer sequence and is the primary source for the inbound answer protocol.

### New or corrected findings vs PCAP1

| Finding | PCAP1/Old assumption | PCAP2/Corrected |
|---------|----------------------|-----------------|
| fresh_ts source | `int(time.time())` | transform(ring_ts): byte[0]\|=0x80; swap byte[2] and byte[3]+1 |
| Device-ts ACK formula | Unknown / ad-hoc | Same transform as fresh_ts, applied to device's ts field |
| Codec retransmit ts | fresh_ts | call_counter (= fresh_ts+B5) |
| Codec retransmit order | Before bundle wait | After ACK2 and simultaneous RTPC2 open |
| RTPC2 open timing | After ACK2 | Simultaneously with ACK2 (~0.372s after ring) |
| encode_answer_peer inbound | Assumed same as outbound | **48 bytes**: `0x0000` padding before separator; caller IS repeated after separator; callee = our_base_addr (NOT entrance_addr) |
| encode_call_accepted extra | Thought `b"\x00\x00\xff\xff"` (6 FF) | **`b"\x00\x00"` (4 FF total = separator only)** |
| Extra ACK between rtpc2_ready and rtpc_link | Unknown | NOT present (no client message between these two) |
| Device signals after call_accepted | Not documented | Device sends 0x1840/0x000A (rtpc_link) and 0x1840/0x000E (PEER); client must ACK each with transform(device_ts) |
| Device-initiated RTPC body | Not documented | 15-byte ABCD: `cd ab 01 00 07 00 00 00 52 54 50 43 01 [ch_id lo] [ch_id hi]`; server_channel_id at body[-3:-1] |
| video_config extra[10:14] | `00 00 00 00` | `C0 00 00 00`; inbound primary/secondary resolution = 320×240 (not 800×480) |
| Media transport (inbound) | Assumed UDP | **TCP** on RTPC1 (audio PT=8) and RTPC2 (video PT=99) |

### Resolution
All findings above verified via byte-comparison against PCAP2 and confirmed with a live end-to-end test (514 video frames + 720 audio frames received successfully).
