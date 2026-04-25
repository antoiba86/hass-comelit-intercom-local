# Changelog

## 0.1.4.2

**Door open fixes:**
- **Fix: standalone door open broken** — ACK pair (`0x1800`/`0x1820`) was sent on all paths; standalone door open must never send it. Added `send_ack=False` parameter to `ctpp_init_sequence`
- **Fix: `read_response_ctpp` was never awaited** — device responses were not drained, leaving stale data in the socket buffer
- **Fix: inverted actuator/door init messages** — swapped ternary caused actuator to send `encode_door_init` and vice versa
- **Fix: missing `await` and wrong class reference** — `open_ctpp_channel` and `_open_door_on_channel` were not awaited; `DeviceConfig` class was passed instead of the config instance
- **Fix: `_CTPP_RESPONSE_MIN_LEN` constant** — replaced magic number `8` in response length guard with a named constant

**Refactor:**
- **Unified door open entry point** — removed `open_door_fast` / `open_door_standalone` / `_open_regular_on_channel` / `_open_actuator_on_channel`; single `open_door` selects fast or standalone path automatically

**Tests:**
- **End-to-end door flow coverage** — new `test_door_flow.py` exercises the full chain with only the TCP client mocked; includes regression test for `send_ack=False`

**Video fixes:**
- **Fix: door open during video triggered CTPP re-establishment** — relay response `0x1840/0x0003` was misclassified as CALL_END; monitor now inspects the sub-field and bare-ACKs relay confirmations
- **Fix: video auto-restarted after door-open ended the call** — user-stopped flag is now set the moment the door button is pressed
- **Fix: `AttributeError` on concurrent `async_stop_video`** — session is now snapshotted and cleared atomically before awaiting stop-callbacks

## 0.1.4.1

- **Fix: door open when notifications are enabled** — restored the full 6-step CTPP sequence that was accidentally simplified in 0.1.4 (added back `encode_door_init` / `encode_actuator_init` between the OPEN/CONFIRM pairs); regular-door and actuator opens now work reliably on the shared CTPP channel
- **Fix: duplicate door_opened warnings** — no longer ACK the `0x1860/0x0003` VIP event; the device retransmits briefly and stops on its own, so the "RETRANSMIT: our previous ACK was not accepted" warnings after each door open are gone

## 0.1.4

> **⚠ Breaking change — entity IDs have changed**
>
> Entity IDs are now derived from the integration's **title** instead of the hardcoded string `"Comelit Intercom"`.
> If you added the integration before this version, your entity IDs may have changed (e.g. from
> `button.comelit_intercom_actuator` to `button.comelit_192_168_1_111_actuator` if no custom name was set).
>
> **Fix:** remove and re-add the integration, giving it a friendly name (e.g. `Front Door`) in the new Name field.
> Entities will then be stable going forward (e.g. `button.front_door_actuator`).

- **Custom integration name** — new optional "Name" field in the config flow sets the integration title and entity prefix; leave blank to use the host IP
- **Options flow** — enable or disable doorbell notifications after setup via Settings → Integrations → Configure without removing and re-adding the integration
- **Reliable doorbell detection** — replaced the FCM-based PUSH mechanism with a persistent CTPP channel listener (VIP events); actual call events are now received as binary messages on the device's local TCP channel, not via cloud FCM
- **Doorbell notification card** — new `comelit-doorbell-card` auto-registered on startup; shows ring alert with Answer/Dismiss buttons and transitions to live stream when answered
- **Door open during active video** — pressing a door button while video is active sends a single message on the existing CTPP channel (PCAP-verified Android app behaviour); no second TCP connection
- **Faster door open** — when notifications are enabled, the CTPP channel is already open so door open skips the init handshake entirely (~30 ms vs ~2 s)
- **Single shared TCP connection** — video signaling, VIP event listening, and door control share the coordinator's TCP connection; eliminates conflicts when the device only accepts one client at a time
- **Door auto-stop** — pressing a door button while video is active automatically stops the video session 10 s later
- **Faster time-to-first-frame** — RTSP `PLAY` response is gated until video RTP is flowing, preventing HA's stream worker from erroring on an empty stream; RTCP Sender Reports eliminate "no reference clock" delays in go2rtc, VLC, and browsers
- **Accurate camera state** — `is_streaming` property reflects the active session so the Lovelace card transitions correctly and go2rtc attaches via WebRTC on the first video session
- **TCP keepalive probe** — push-info re-sent every 90 s keeps the connection alive during idle periods; prevents false reconnect cycles when the device is reachable but quiet

## 0.1.3

- **Video renewal** — inline re-establishment on CALL_END (~30s) without TCP reconnect; video is uninterrupted
- **Custom Lovelace card** — play-button UI auto-registered on HA startup; no manual resource configuration needed
- **Concurrent session protection** — a second video start while one is in progress is immediately rejected, preventing CTPP negotiation conflicts
- **TCP video fallback** — video works via TCP (RTPC2) when UDP is blocked by NAT/firewall
- **Consistent entity naming** — all entities use the `comelit_intercom_` prefix (e.g., `button.comelit_intercom_actuator`, `camera.comelit_intercom_live_feed`)
