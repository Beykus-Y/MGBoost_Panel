"""PH2-06 scoped abuse-control rate limiter for public subscription-fetch
endpoints (`GET /sub/{legacy_token}` and the PH4-04 opaque root route).

Single-process, in-memory sliding window -- the same architecture class as
`security.AdminLoginRateLimiter` (PH8-02 owns any future shared-state
multi-worker migration; this project's production server is intentionally
single-process today). Limited by client IP only, resolved through the
already-existing trusted-XFF boundary (`http_utils.client_ip`) -- never by
raw token or token hash, so a malformed/unknown-token flood from one IP is
bounded by the exact same per-IP budget as any other request from that IP,
and this module never stores a raw token, a token hash, or any other
per-request identity beyond the client IP itself.

A rejected (over-limit) request is NOT counted against the bucket again --
only requests that were actually allowed consume budget. This keeps each
IP's bucket bounded at exactly `max_requests` entries even under an
indefinite flood (no unbounded growth), and it does not let a client
extend its own block by continuing to send requests during it.

Fail-closed by construction: the limiter is a plain in-memory dict guarded
by a lock -- there is no external store/network call that could fail open.
A full `_MAX_TRACKED_IPS` table evicts the least-recently-active IP before
inserting a new one, so total memory is bounded regardless of how many
distinct IPs are seen.
"""

from __future__ import annotations

import threading
import time


_MAX_TRACKED_IPS = 20_000

# Deliberately conservative technical defaults, not a product decision:
# generous enough that no real subscription client's normal auto-refresh or
# manual-retry behavior is ever throttled, tight enough to bound a scripted
# flood's share of the single-threaded server's request-handling budget.
DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_MAX_REQUESTS = 30


class SubscriptionRateLimiter:
    def __init__(
        self, *, window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
    ):
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_requests = max(1, int(max_requests))
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _prune(bucket: list[float], now: float, window: float) -> None:
        cutoff = now - window
        bucket[:] = [timestamp for timestamp in bucket if timestamp > cutoff]

    @staticmethod
    def _evict_oldest(store: dict, maximum: int) -> None:
        while len(store) >= maximum:
            oldest = min(store.items(), key=lambda item: item[1][-1] if item[1] else 0.0)[0]
            store.pop(oldest, None)

    def check(self, ip: str, *, now: float | None = None) -> int:
        """Returns 0 if this request is allowed (and records it). Returns a
        positive integer `Retry-After` seconds value if the caller must
        reject this request instead -- the request is NOT recorded in that
        case."""
        checked_at = time.time() if now is None else now
        key = ip if isinstance(ip, str) and ip else "0.0.0.0"
        with self._lock:
            bucket = self._buckets.get(key, [])
            self._prune(bucket, checked_at, self.window_seconds)
            if len(bucket) >= self.max_requests:
                if bucket:
                    self._buckets[key] = bucket
                else:
                    self._buckets.pop(key, None)
                return max(1, int(bucket[0] + self.window_seconds - checked_at + 0.999))
            self._evict_oldest(self._buckets, _MAX_TRACKED_IPS)
            bucket.append(checked_at)
            self._buckets[key] = bucket
            return 0

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()

    def tracked_ip_count(self) -> int:
        with self._lock:
            return len(self._buckets)


SUBSCRIPTION_FETCH_LIMITER = SubscriptionRateLimiter()
