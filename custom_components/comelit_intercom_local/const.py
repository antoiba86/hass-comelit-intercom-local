"""Constants for the Comelit Local integration."""

DOMAIN = "comelit_intercom_local"
MANUFACTURER = "Comelit"
MODEL = "6701W"

CONF_HTTP_PORT = "http_port"
CONF_VIDEO_AUTO_RECONNECT = "video_auto_reconnect"
CONF_ENABLE_NOTIFICATIONS = "enable_notifications"
CONF_VERBOSE_LOGGING = "verbose_logging"

_verbose_logging: bool = False


def is_verbose_logging() -> bool:
    """Return True when verbose logging is enabled via the integration options."""
    return _verbose_logging


def set_verbose_logging(enabled: bool) -> None:
    """Set the verbose logging flag (called on setup and options reload)."""
    global _verbose_logging
    _verbose_logging = enabled

DEFAULT_PORT = 64100
DEFAULT_HTTP_PORT = 8080

# Video config sent to the device via encode_video_config().
VIDEO_WIDTH = 800
VIDEO_HEIGHT = 480
VIDEO_FPS = 16

