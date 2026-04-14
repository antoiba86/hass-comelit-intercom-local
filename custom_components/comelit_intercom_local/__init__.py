"""Comelit Local integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, MAJOR_VERSION, MINOR_VERSION, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import DEFAULT_PORT, DOMAIN
from .coordinator import ComelitLocalConfigEntry, ComelitLocalCoordinator
from .exceptions import (
    AuthenticationError,
    ConnectionComelitError as ComelitConnectionError,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.CAMERA, Platform.EVENT]

_CARD_URL = "/comelit_intercom_local/comelit-intercom-card.js"
_CARD_PATH = str(Path(__file__).parent / "www" / "comelit-intercom-card.js")

_DOORBELL_CARD_URL = "/comelit_intercom_local/comelit-doorbell-card.js"
_DOORBELL_CARD_PATH = str(Path(__file__).parent / "www" / "comelit-doorbell-card.js")


async def _register_static_path(hass: HomeAssistant, url: str, path: str) -> None:
    """Register a static file path, compatible with all HA versions."""
    if (MAJOR_VERSION, MINOR_VERSION) >= (2024, 7):
        from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415
        await hass.http.async_register_static_paths(
            [StaticPathConfig(url, path, cache_headers=True)]
        )
    else:
        hass.http.register_static_path(url, path)


async def _init_resource(hass: HomeAssistant, url: str, version: str) -> None:
    """Add the card JS to Lovelace resources (GUI mode) or extra JS (YAML mode)."""
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415
    from homeassistant.components.lovelace.resources import ResourceStorageCollection  # noqa: PLC0415

    lovelace = hass.data["lovelace"]
    resources: ResourceStorageCollection = (
        lovelace.resources if hasattr(lovelace, "resources") else lovelace["resources"]
    )
    await resources.async_get_info()

    url_versioned = f"{url}?v={version}"

    for item in resources.async_items():
        if not item.get("url", "").startswith(url):
            continue
        if item["url"].endswith(version):
            return  # already up to date
        if isinstance(resources, ResourceStorageCollection):
            await resources.async_update_item(
                item["id"], {"res_type": "module", "url": url_versioned}
            )
        else:
            item["url"] = url_versioned
        _LOGGER.debug("Updated Lovelace resource to %s", url_versioned)
        return

    if isinstance(resources, ResourceStorageCollection):
        await resources.async_create_item({"res_type": "module", "url": url_versioned})
        _LOGGER.debug("Added Lovelace resource: %s", url_versioned)
    else:
        add_extra_js_url(hass, url_versioned)
        _LOGGER.debug("Added extra JS module: %s", url_versioned)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the Lovelace card static files and add them to resources."""
    version = getattr(hass.data.get("integrations", {}).get(DOMAIN), "version", "0")
    for url, path in (
        (_CARD_URL, _CARD_PATH),
        (_DOORBELL_CARD_URL, _DOORBELL_CARD_PATH),
    ):
        await _register_static_path(hass, url, path)
        await _init_resource(hass, url, str(version))
        _LOGGER.info("Comelit card registered at %s", url)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: ComelitLocalConfigEntry
) -> bool:
    """Set up Comelit Local from a config entry."""
    coordinator = ComelitLocalCoordinator(
        hass,
        entry,
        host=entry.data[CONF_HOST],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        token=entry.data[CONF_TOKEN],
    )

    try:
        await coordinator.async_setup()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed(
            f"Authentication failed for Comelit device: {err}"
        ) from err
    except (TimeoutError, ComelitConnectionError, OSError) as err:
        raise ConfigEntryNotReady(
            f"Failed to connect to Comelit device: {err}"
        ) from err
    except Exception as err:
        raise ConfigEntryNotReady(
            f"Unexpected error setting up Comelit device: {err}"
        ) from err

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: ComelitLocalConfigEntry
) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: ComelitLocalConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok
