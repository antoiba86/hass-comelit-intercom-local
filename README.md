# Comelit Intercom Local

Home Assistant custom component for the **Comelit 6701W** WiFi video intercom. Communicates via the ICONA Bridge TCP protocol — no cloud required.

## Features

- **Remote door opening** — open doors/gates from Home Assistant
- **Live intercom video** — view the door camera stream directly in HA dashboards via local RTSP
- **Doorbell events** — automations trigger on ring or missed call
- **Custom Lovelace card** — play-button UI auto-registered on startup; starts video on click, stops on navigation away
- **100% local** — all communication stays on your LAN, no cloud required

## Requirements

- Comelit 6701W (or compatible ICONA Bridge device)
- Device accessible on your local network
- Home Assistant 2026.1+

## Installation

### HACS (Recommended)

1. Add this repository as a custom repository in HACS
2. Install **Comelit Intercom Local**
3. Restart Home Assistant

### Manual

1. Copy the `custom_components/comelit_intercom_local/` folder to your HA `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Comelit Intercom Local**
3. Enter your device IP and either:
   - Your device password (token will be extracted automatically), or
   - A pre-extracted 32-character hex token

## Entities

| Entity | Description |
|--------|-------------|
| `button.comelit_intercom_<door_name>` | Press to open a door or gate (e.g., `button.comelit_intercom_actuator`) |
| `button.comelit_intercom_start_video_feed` | Manually start the intercom video call |
| `button.comelit_intercom_stop_video_feed` | Stop the active video call |
| `camera.comelit_intercom_live_feed` | Live video stream from the door panel via local RTSP. Auto-starts on doorbell ring. |
| `camera.comelit_intercom_<name>` | RTSP stream from each additional configured camera |
| `event.comelit_intercom_doorbell` | Fires `doorbell_ring` and `missed_call` events for automations |

### Lovelace Card

A custom card is automatically registered on startup. Add it to your dashboard:

```yaml
type: custom:comelit-intercom-card
camera_entity: camera.comelit_intercom_live_feed
start_entity: button.comelit_intercom_start_video_feed  # optional
stop_entity: button.comelit_intercom_stop_video_feed
```

The card shows a camera snapshot with a play button overlay. Click play to start the video feed. Video stops automatically when you navigate away.

### Automation Example

```yaml
automation:
  - alias: "Notify on doorbell ring"
    trigger:
      - platform: state
        entity_id: event.comelit_intercom_doorbell
        attribute: event_type
        to: "doorbell_ring"
    action:
      - service: notify.mobile_app
        data:
          message: "Someone is at the door!"
```

## Protocol

The ICONA Bridge protocol runs over raw TCP on port 64100. Every message has an 8-byte header:

```
[0x00 0x06] [body_length LE16] [request_id LE16] [0x00 0x00]
```

Key operations:
- **Authentication**: Open UAUT channel → send JSON access request with token → expect code 200
- **Configuration**: Open UCFG channel → request config → parse doors, cameras, addresses
- **Door open**: Open CTPP channel → 6-step binary sequence (init → open+confirm → door init → open+confirm)
- **Push notifications**: Open PUSH channel → receive unsolicited JSON on doorbell ring

## Changelog

### 0.1.3
- **Video renewal** — inline re-establishment on CALL_END (~30s) without TCP reconnect; video is uninterrupted
- **Custom Lovelace card** — play-button UI auto-registered on HA startup; no manual resource configuration needed
- **Concurrent session protection** — a second video start while one is in progress is immediately rejected, preventing CTPP negotiation conflicts
- **TCP video fallback** — video works via TCP (RTPC2) when UDP is blocked by NAT/firewall
- **Consistent entity naming** — all entities use the `comelit_intercom_` prefix (e.g., `button.comelit_intercom_actuator`, `camera.comelit_intercom_live_feed`)

## Acknowledgments

Protocol knowledge derived from community reverse-engineering efforts:
- [ha-component-comelit-intercom](https://github.com/nicolas-fricke/ha-component-comelit-intercom) by Nicolas Fricke
- [comelit-client](https://github.com/madchicken/comelit-client) by Pierpaolo Follia
- [Protocol analysis](https://grdw.nl/2023/01/28/my-intercom-part-1.html) by grdw

## License

Apache 2.0
