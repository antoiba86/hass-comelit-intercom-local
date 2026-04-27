"""Constants for the Comelit Local integration."""

DOMAIN = "comelit_intercom_local"
MANUFACTURER = "Comelit"
MODEL = "6701W"

CONF_HTTP_PORT = "http_port"
CONF_VIDEO_AUTO_RECONNECT = "video_auto_reconnect"
CONF_ENABLE_NOTIFICATIONS = "enable_notifications"
CONF_VERBOSE_LOGGING = "verbose_logging"

# Sub-loggers that are extremely chatty at DEBUG (per-packet wire dumps,
# RTP frame logs, RTSP frame logs). When verbose_logging is OFF, these
# are pinned to INFO regardless of HA's logger config.
NOISY_SUBLOGGERS = ("client", "rtp_receiver", "rtsp_server")

DEFAULT_PORT = 64100
DEFAULT_HTTP_PORT = 8080

# Video config sent to the device via encode_video_config().
VIDEO_WIDTH = 800
VIDEO_HEIGHT = 480
VIDEO_FPS = 16

