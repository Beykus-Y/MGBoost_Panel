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
COMPAT_TELEMETRY_HMAC_KEY = os.getenv("COMPAT_TELEMETRY_HMAC_KEY", "")
# Dedicated key for durable slot verifiers. It remains unused until an
# explicitly approved PH3 canary claim is executed.
DEVICE_SLOT_HMAC_KEY = os.getenv("DEVICE_SLOT_HMAC_KEY", "")
# Stable, non-secret actor identifier used only by the dormant PH3-06
# entitlement write boundary.  An empty value deliberately disables all
# internal-plan/account/override mutations; it does not affect legacy runtime.
PRIMARY_MGBOOST_ADMIN_ACTOR_ID = os.getenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", "").strip()
# Login identity mapped to the stable actor only after successful server-side
# Marzban authentication. Both values must be configured or privileged PH3
# mutations remain disabled. Neither may be supplied by an HTTP request.
PRIMARY_MGBOOST_ADMIN_LOGIN = os.getenv("PRIMARY_MGBOOST_ADMIN_LOGIN", "").strip()
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")
INTERNAL_API_ALLOWED_SKEW_SECONDS = int(os.getenv("INTERNAL_API_ALLOWED_SKEW_SECONDS", "300"))
INTERNAL_API_IDEMPOTENCY_TTL_SECONDS = int(
    os.getenv("INTERNAL_API_IDEMPOTENCY_TTL_SECONDS", "604800")
)
INTERNAL_API_REQUIRE_V2_MUTATIONS = os.getenv(
    "INTERNAL_API_REQUIRE_V2_MUTATIONS", "0"
).strip().lower() in {"1", "true", "yes", "on"}
SUB_BROWSER_CSP_ENFORCE = os.getenv(
    "SUB_BROWSER_CSP_ENFORCE", "0"
).strip().lower() in {"1", "true", "yes", "on"}
ADMIN_SESSION_TTL_SECONDS = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", "1800"))
ADMIN_SESSION_COOKIE_SECURE = os.getenv("ADMIN_SESSION_COOKIE_SECURE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
ADMIN_LOGIN_RATE_WINDOW_SECONDS = int(os.getenv("ADMIN_LOGIN_RATE_WINDOW_SECONDS", "300"))
ADMIN_LOGIN_RATE_IDENTITY_FAILURES = int(os.getenv("ADMIN_LOGIN_RATE_IDENTITY_FAILURES", "5"))
ADMIN_LOGIN_RATE_IP_FAILURES = int(os.getenv("ADMIN_LOGIN_RATE_IP_FAILURES", "20"))
# PH3-03 dual-run shadow resolver. Disabled by default; even when enabled it
# only ever runs in parallel with the legacy response and can never replace
# or delay it (see docs/PHASE3_CHILD_PROVISIONING.md). It requires its own
# narrow broker credential — MARZBAN_BROKER_RESOLVER_AUTH_KEY,
# MARZBAN_BROKER_RESOLVER_CLIENT_ID (default mgboost-sub-resolver) and
# MARZBAN_BROKER_RESOLVER_TIMEOUT_SECONDS are read directly by
# src/shadow_resolver.py, mirroring the existing MARZBAN_BROKER_* variables.
SHADOW_RESOLVER_ENABLED = os.getenv("SHADOW_RESOLVER_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}
# PH3-04 HWID fail-closed compatibility gate (src/hwid_gate.py, src/compat_registry.py).
# Dormant: no legacy route or resolver imports src/hwid_gate.py, so this flag
# currently has no runtime effect at all -- it exists only so a future,
# separately approved caller (the PH4 migration path) has a staged rollout
# knob instead of a single on/off switch. OFF is the only production value
# in scope for PH3-04 itself.
#   OFF      - default; identical to "the gate does not exist".
#   CANARY   - reserved for a future explicitly-scoped, non-global evaluation.
#   ENFORCE  - reserved for the future migration window; never set in PH3-04.
PH3_04_ENFORCEMENT_MODE = os.getenv("PH3_04_ENFORCEMENT_MODE", "OFF").strip().upper()
if PH3_04_ENFORCEMENT_MODE not in {"OFF", "CANARY", "ENFORCE"}:
    PH3_04_ENFORCEMENT_MODE = "OFF"
