import os
from dotenv import load_dotenv

# The isolated Marzban broker receives its complete environment from a
# root-managed systemd EnvironmentFile and must not read the main process'
# repository .env.  This also lets the broker use a separate Unix identity
# which has no filesystem permission to that file.
if os.getenv("MGBOOST_SKIP_DOTENV", "0") != "1":
    load_dotenv()

MARZBAN_URL = os.getenv("MARZBAN_URL", "http://127.0.0.1:8000")
# Public hostname used to build links sent outside of an HTTP request context
# (e.g. Telegram bot messages), where there is no incoming Host header to
# read from. Required — no hardcoded domain default. Deployments that omit
# it get an empty string here; callers that need it (see
# bot_support._build_management_link) must check for that and fail closed
# with a clear error rather than silently building a broken/wrong-domain
# link.
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "")
LISTEN_HOST = os.getenv("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8001"))
DATA_DIR = os.getenv("DATA_DIR", "./data")
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")
INTERNAL_API_ALLOWED_SKEW_SECONDS = int(os.getenv("INTERNAL_API_ALLOWED_SKEW_SECONDS", "300"))
ADMIN_SESSION_TTL_SECONDS = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", "1800"))
ADMIN_SESSION_COOKIE_SECURE = os.getenv("ADMIN_SESSION_COOKIE_SECURE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
ADMIN_LOGIN_RATE_WINDOW_SECONDS = int(os.getenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "300"))
ADMIN_LOGIN_RATE_IDENTITY_FAILURES = int(os.getenv("ADMIN_LOGIN_RATE_IDENTITY_FAILURES", "5"))
ADMIN_LOGIN_RATE_IP_FAILURES = int(os.getenv("ADMIN_LOGIN_RATE_IP_FAILURES", "20"))
