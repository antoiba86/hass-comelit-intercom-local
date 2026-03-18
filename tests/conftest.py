"""Shared test fixtures."""

import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Real exception classes that HA code raises / catches.
# These must be *real* classes (not MagicMock) so raise/except work correctly.
# ---------------------------------------------------------------------------


class _UpdateFailed(Exception):
    """Stand-in for homeassistant.helpers.update_coordinator.UpdateFailed."""


class _ConfigEntryNotReady(Exception):
    """Stand-in for homeassistant.exceptions.ConfigEntryNotReady."""


# ---------------------------------------------------------------------------
# Minimal DataUpdateCoordinator stub so ComelitLocalCoordinator can inherit.
# ---------------------------------------------------------------------------


class _DataUpdateCoordinator:
    """Minimal stub for homeassistant.helpers.update_coordinator.DataUpdateCoordinator."""

    def __class_getitem__(cls, item):
        """Allow DataUpdateCoordinator[T] syntax."""
        return cls

    def __init__(self, hass, logger, *, name, update_interval):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval

    def async_set_updated_data(self, data):
        """No-op in tests."""


# ---------------------------------------------------------------------------
# Minimal ConfigFlow / ConfigFlowResult stubs for config_flow tests.
# ---------------------------------------------------------------------------


class _ConfigFlowResult(dict):
    """Stub for ConfigFlowResult — just a dict."""


class _ConfigFlow:
    """Stub for homeassistant.config_entries.ConfigFlow."""

    domain: str = ""

    def __init_subclass__(cls, domain: str = "", **kwargs):
        super().__init_subclass__(**kwargs)
        cls.domain = domain

    async def async_set_unique_id(self, uid):
        pass

    def _abort_if_unique_id_configured(self):
        pass

    def async_create_entry(self, *, title, data):
        return _ConfigFlowResult(type="create_entry", title=title, data=data)

    def async_show_form(self, *, step_id, data_schema, errors):
        return _ConfigFlowResult(
            type="form", step_id=step_id, data_schema=data_schema, errors=errors
        )


# ---------------------------------------------------------------------------
# Mock homeassistant modules so unit tests can import library code
# from custom_components.comelit_local without requiring HA installed.
# ---------------------------------------------------------------------------

# Build mock modules, injecting real classes where needed.
_ha_exceptions = MagicMock()
_ha_exceptions.ConfigEntryNotReady = _ConfigEntryNotReady

_ha_update_coordinator = MagicMock()
_ha_update_coordinator.DataUpdateCoordinator = _DataUpdateCoordinator
_ha_update_coordinator.UpdateFailed = _UpdateFailed

_ha_config_entries = MagicMock()
_ha_config_entries.ConfigFlow = _ConfigFlow
_ha_config_entries.ConfigFlowResult = _ConfigFlowResult

_ha_const = MagicMock()
# Provide real string constants that the component uses
_ha_const.CONF_HOST = "host"
_ha_const.CONF_PORT = "port"
_ha_const.CONF_TOKEN = "token"
_ha_const.CONF_PASSWORD = "password"
_ha_const.Platform = MagicMock()
_ha_const.Platform.BUTTON = "button"
_ha_const.Platform.CAMERA = "camera"
_ha_const.Platform.EVENT = "event"

# Create the top-level homeassistant mock first, then wire child attributes
_ha = MagicMock()
_ha_helpers = MagicMock()

# Wire child modules as attributes on their parents
_ha.config_entries = _ha_config_entries
_ha.const = _ha_const
_ha.core = MagicMock()
_ha.exceptions = _ha_exceptions
_ha.helpers = _ha_helpers
_ha_helpers.update_coordinator = _ha_update_coordinator

# Stub for homeassistant.components.camera
_ha_camera = MagicMock()


class _CameraEntityFeature:
    STREAM = 1
    ON_OFF = 2


class _Camera:
    """Minimal stub for homeassistant.components.camera.Camera."""

    _attr_has_entity_name = False
    _attr_name = None
    _attr_unique_id = None
    _attr_icon = None
    _attr_supported_features = 0

    def __init__(self):
        pass

    def async_write_ha_state(self):
        pass


_ha_camera.Camera = _Camera
_ha_camera.CameraEntityFeature = _CameraEntityFeature

_ha_entity_platform = MagicMock()

# Register all modules in sys.modules
sys.modules["homeassistant"] = _ha
sys.modules["homeassistant.config_entries"] = _ha_config_entries
sys.modules["homeassistant.const"] = _ha_const
sys.modules["homeassistant.core"] = _ha.core
sys.modules["homeassistant.exceptions"] = _ha_exceptions
sys.modules["homeassistant.helpers"] = _ha_helpers
sys.modules["homeassistant.helpers.update_coordinator"] = _ha_update_coordinator
_ha_helpers_entity = MagicMock()
_ha_helpers_entity.DeviceInfo = dict  # DeviceInfo is dict-like

sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.camera"] = _ha_camera
sys.modules["homeassistant.helpers.entity"] = _ha_helpers_entity
sys.modules["homeassistant.helpers.entity_platform"] = _ha_entity_platform

import pytest


@pytest.fixture
def sample_apt_address() -> str:
    return "00000001"


@pytest.fixture
def sample_token() -> str:
    return "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
