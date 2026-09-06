import os
import ipaddress
import re
from urllib.parse import urlsplit
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


def subscription_base_url() -> str | None:
    """Base URL for opaque subscription links handed to users outside an
    HTTP request context (Telegram delivery, LK issuance response). Returns
    None when PUBLIC_HOST is not configured -- callers must fail closed with
    a clear error instead of silently building a wrong-domain link."""
    host = (PUBLIC_HOST or "").strip().rstrip("/")
    if not host or any(char.isspace() for char in host):
        return None
    # PUBLIC_HOST is deliberately a host[:port], not an arbitrary URL: this
    # helper owns the HTTPS scheme and every caller appends an opaque path.
    # Rejecting a supplied scheme/path/query prevents links such as
    # ``https://https://example.test/token`` and accidental host injection.
    if any(char in host for char in "/?#@"):
        return None
    try:
        parsed = urlsplit(f"//{host}")
        if not parsed.hostname or parsed.username or parsed.password:
            return None
        _ = parsed.port  # validates a supplied port (and raises on bad input)
    except ValueError:
        return None
    hostname = parsed.hostname
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if hostname != "localhost" and (
            len(hostname) > 253
            or any(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
                for label in hostname.split(".")
            )
        ):
            return None
    return f"https://{host}"
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

# PH2-01 opaque subscription resolver (src/opaque_resolver.py, routes/opaque_sub.py).
# Dormant by default. Even when this is left at its default OFF, the route is
# already unreachable in production because sub.beykus.fun's nginx vhost has
# no root `location /` proxying to the panel (only /sub/, /lk/, /assets/,
# /internal/, /sub-admin*) -- this flag is defense-in-depth, not the only gate.
#   OFF     - default; the root token route always returns the uniform
#             invalid-subscription response, regardless of DB state.
#   ENABLED - reserved for a future explicitly-scoped rollout gate.
OPAQUE_SUBSCRIPTION_ENABLED = os.getenv("OPAQUE_SUBSCRIPTION_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}

# PH4-01 legacy subscription alias bridge (src/legacy_bridge_resolver.py).
# Dormant by two independent gates: this flag defaults OFF, and even when
# on, `LegacyBridgeStore.resolve_account_for_legacy_username` only ever
# returns an account for one with an explicit, root-only-created
# `enabled=1` mgboost_legacy_bridge_bindings row -- in production today
# there are zero such rows, so turning this flag on alone changes nothing.
#   OFF     - default; legacy /sub is byte-identical to pre-PH4-01 behavior.
#   ENABLED - reserved for a future explicit, owner-approved canary gate.
LEGACY_BRIDGE_ENABLED = os.getenv("LEGACY_BRIDGE_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}

# BUG-002 fix / PH6-08 readiness gate (src/wl_packages.py::assert_wl_package_sales_enabled).
# WL packages (+50/100/250/500 GB) are fully catalogued/priced (PH5-03) and
# technically purchasable end-to-end through the manual (RUB) admin payment
# route, but the owner has not launched this as a supported customer-facing
# feature: the effective-quota contract that would make a sold package
# actually retain WL access (PH6-08) does not exist yet -- WL enforcement
# still decides only from the base quota (see BUGS.md BUG-002). The Stars
# channel already never lists packages in its sellable catalog for the same
# reason; this flag is the single source of truth for the manual channel so
# the two channels cannot silently diverge again.
#   OFF (default) - preview/catalog/create all fail closed for any WL_PACKAGE
#                   product on every channel; existing/historical package
#                   rows and grants are entirely unaffected either way.
#   ON             - reserved for the future explicit rollout once PH6-08's
#                     effective-quota enforcement exists; never set today.
WL_PACKAGE_SALES_ENABLED = os.getenv("WL_PACKAGE_SALES_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on",
}
