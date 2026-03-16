"""Real device integration tests.

Run with: COMELIT_HOST=192.168.1.XX COMELIT_TOKEN=<token> pytest tests/test_integration.py -v
Set COMELIT_PASSWORD to auto-extract token via HTTP backup.
"""

import asyncio
import os

import pytest

COMELIT_HOST = os.environ.get("COMELIT_HOST")
COMELIT_TOKEN = os.environ.get("COMELIT_TOKEN")
COMELIT_PASSWORD = os.environ.get("COMELIT_PASSWORD", "comelit")

pytestmark = pytest.mark.skipif(
    not COMELIT_HOST, reason="COMELIT_HOST not set (real device required)"
)


@pytest.mark.asyncio
async def test_extract_token():
    """Extract token from device backup."""
    pytest.importorskip("aiohttp")
    from custom_components.comelit_local.token import extract_token

    token = await extract_token(COMELIT_HOST, password=COMELIT_PASSWORD)
    assert len(token) == 32
    assert all(c in "0123456789abcdef" for c in token)
    print(f"Extracted token: {token}")


@pytest.mark.asyncio
async def test_connect_and_authenticate():
    """Connect and authenticate with the device."""
    if not COMELIT_TOKEN:
        pytest.skip("COMELIT_TOKEN not set")

    from custom_components.comelit_local.client import IconaBridgeClient
    from custom_components.comelit_local.auth import authenticate

    client = IconaBridgeClient(COMELIT_HOST)
    await client.connect()
    try:
        await authenticate(client, COMELIT_TOKEN)
        assert client.connected
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_get_config():
    """Fetch device configuration."""
    if not COMELIT_TOKEN:
        pytest.skip("COMELIT_TOKEN not set")

    from custom_components.comelit_local.client import IconaBridgeClient
    from custom_components.comelit_local.auth import authenticate
    from custom_components.comelit_local.config_reader import get_device_config

    client = IconaBridgeClient(COMELIT_HOST)
    await client.connect()
    try:
        await authenticate(client, COMELIT_TOKEN)
        config = await get_device_config(client)
        print(f"Apt address: {config.apt_address}")
        print(f"Doors: {[d.name for d in config.doors]}")
        print(f"Cameras: {[c.name for c in config.cameras]}")
        assert config.apt_address
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_open_door():
    """Open the first door (CAREFUL: this actually opens a door!)."""
    if not COMELIT_TOKEN:
        pytest.skip("COMELIT_TOKEN not set")
    if not os.environ.get("COMELIT_TEST_DOOR"):
        pytest.skip("Set COMELIT_TEST_DOOR=1 to actually open a door")

    from custom_components.comelit_local.client import IconaBridgeClient
    from custom_components.comelit_local.auth import authenticate
    from custom_components.comelit_local.config_reader import get_device_config
    from custom_components.comelit_local.door import open_door
    from custom_components.comelit_local.protocol import ICONA_BRIDGE_PORT

    client = IconaBridgeClient(COMELIT_HOST)
    await client.connect()
    try:
        await authenticate(client, COMELIT_TOKEN)
        config = await get_device_config(client)
        assert config.doors, "No doors found in config"
        door = config.doors[0]
        print(f"Opening door: {door.name}")
        await open_door(COMELIT_HOST, ICONA_BRIDGE_PORT, COMELIT_TOKEN, config, door)
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_push_listener():
    """Listen for push notifications for 30 seconds."""
    if not COMELIT_TOKEN:
        pytest.skip("COMELIT_TOKEN not set")
    if not os.environ.get("COMELIT_TEST_PUSH"):
        pytest.skip("Set COMELIT_TEST_PUSH=1 to listen for push events")

    from custom_components.comelit_local.client import IconaBridgeClient
    from custom_components.comelit_local.auth import authenticate
    from custom_components.comelit_local.config_reader import get_device_config
    from custom_components.comelit_local.push import register_push

    events = []

    client = IconaBridgeClient(COMELIT_HOST)
    await client.connect()
    try:
        await authenticate(client, COMELIT_TOKEN)
        config = await get_device_config(client)
        await register_push(client, config, lambda e: events.append(e))
        print("Listening for push events for 30 seconds... ring the doorbell!")
        await asyncio.sleep(30)
        print(f"Received {len(events)} events: {events}")
    finally:
        await client.disconnect()
