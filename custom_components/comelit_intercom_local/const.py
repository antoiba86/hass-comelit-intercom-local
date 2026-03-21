"""Constants for the Comelit Local integration."""

DOMAIN = "comelit_intercom_local"
MANUFACTURER = "Comelit"
MODEL = "6701W"

CONF_HTTP_PORT = "http_port"
CONF_VIDEO_AUTO_RECONNECT = "video_auto_reconnect"

DEFAULT_PORT = 64100
DEFAULT_HTTP_PORT = 8080

# Device lease timer is ~30s (CALL_END). Pre-warm the next session at t=25s
# so it's ready (~1.3s to establish) before CALL_END arrives (~3.7s margin).
PREWARM_DELAY_SECONDS = 25.0
