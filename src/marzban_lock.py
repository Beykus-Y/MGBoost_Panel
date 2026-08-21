import asyncio
import threading


class PerUsernameLock:
    """One asyncio.Lock per marzban_username, created on first use, shared by
    every write path in this process that needs to read-decide-write a given
    user's Marzban state without another in-process writer interleaving.
    NOT a durability mechanism — purely in-process mutual exclusion for the
    current run of this process. See Phase 2 design doc §5.2/§5.3 for what
    this does and does not guarantee.

    Scope discipline: one marzban_username blocks only itself — two
    different usernames' critical sections run fully concurrently. This is
    an in-process mechanism only; it does not (and cannot) coordinate with a
    hypothetical second process or a direct external edit to Marzban."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = threading.Lock()  # guards dict mutation only,
                                                  # not held during the caller's
                                                  # critical section itself

    def get(self, marzban_username: str) -> asyncio.Lock:
        with self._registry_lock:
            lock = self._locks.get(marzban_username)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[marzban_username] = lock
            return lock


marzban_user_locks = PerUsernameLock()  # module-level singleton
