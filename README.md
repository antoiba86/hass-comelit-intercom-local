# Comelit Local

Home Assistant custom component for the **Comelit 6701W** WiFi video intercom. Communicates via the ICONA Bridge TCP protocol — no cloud required.

## Features

- **Doorbell notifications** — real-time push events when someone rings
- **Remote door opening** — open doors/gates from Home Assistant
- **Camera streaming** — RTSP video feed in HA dashboards
- **100% local** — all communication stays on your LAN

## Requirements

- Comelit 6701W (or compatible ICONA Bridge device)
- Device accessible on your local network
- Home Assistant 2024.1+

## Installation

### HACS (Recommended)

1. Add this repository as a custom repository in HACS
2. Install **Comelit Local**
3. Restart Home Assistant

### Manual

1. Copy the `custom_components/comelit_local/` folder to your HA `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Comelit Local**
3. Enter your device IP and either:
   - Your device password (token will be extracted automatically), or
   - A pre-extracted 32-character hex token

## Entities

| Entity Type | Description |
|-------------|-------------|
| **Button** | One per door — press to open |
| **Camera** | RTSP stream from each configured camera |
| **Event** | Fires `doorbell_ring` and `missed_call` events for automations |

### Automation Example

```yaml
automation:
  - alias: "Notify on doorbell ring"
    trigger:
      - platform: state
        entity_id: event.comelit_local_doorbell
        attribute: event_type
        to: "doorbell_ring"
    action:
      - service: notify.mobile_app
        data:
          message: "Someone is at the door!"
```

## Project Structure

```
custom_components/
  comelit_local/
    __init__.py          # HA integration setup
    config_flow.py       # UI setup with auto token extraction
    coordinator.py       # DataUpdateCoordinator
    button.py            # Door open buttons
    camera.py            # RTSP camera entities
    event.py             # Doorbell ring events
    protocol.py          # Wire protocol: headers, message types, binary payloads
    channels.py          # Channel definitions (UAUT, UCFG, CTPP, PUSH)
    client.py            # AsyncIO TCP client
    auth.py              # Authentication flow
    token.py             # Token extraction from device HTTP backup
    config_reader.py     # Device configuration retrieval
    door.py              # Door open sequence
    push.py              # Push notification listener
    camera_utils.py      # RTSP URL discovery
    models.py            # Data models (Door, Camera, DeviceConfig, PushEvent)
    exceptions.py        # Custom exceptions

tests/                   # Unit and integration tests
```

## Testing

```bash
# Unit tests (no device needed)
PYTHONPATH=. pytest tests/test_protocol.py tests/test_client.py -v

# Integration tests (real device)
COMELIT_HOST=192.168.31.XX COMELIT_TOKEN=<token> \
  pytest tests/test_integration.py -v -s

# Test door opening (actually opens a door!)
COMELIT_HOST=192.168.1.XX COMELIT_TOKEN=<token> COMELIT_TEST_DOOR=1 \
  pytest tests/test_integration.py::test_open_door -v -s

# Listen for doorbell events (30 seconds)
COMELIT_HOST=192.168.1.XX COMELIT_TOKEN=<token> COMELIT_TEST_PUSH=1 \
  pytest tests/test_integration.py::test_push_listener -v -s
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

## Acknowledgments

Protocol knowledge derived from community reverse-engineering efforts:
- [ha-component-comelit-intercom](https://github.com/nicolas-fricke/ha-component-comelit-intercom) by Nicolas Fricke
- [comelit-client](https://github.com/madchicken/comelit-client) by Pierpaolo Follia
- [Protocol analysis](https://grdw.nl/2023/01/28/my-intercom-part-1.html) by grdw

## License

MIT
