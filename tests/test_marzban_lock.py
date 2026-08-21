import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_same_username_serializes():
    from src.marzban_lock import PerUsernameLock

    async def scenario():
        locks = PerUsernameLock()
        events = []

        async def worker(name, delay):
            lock = locks.get("alice")
            async with lock:
                events.append(f"{name}:enter")
                await asyncio.sleep(delay)
                events.append(f"{name}:exit")

        await asyncio.gather(worker("A", 0.05), worker("B", 0.0))
        return events

    events = asyncio.run(scenario())
    # Whichever task acquires first must fully exit before the other enters —
    # no interleaving of enter/exit for the SAME username.
    assert events in (
        ["A:enter", "A:exit", "B:enter", "B:exit"],
        ["B:enter", "B:exit", "A:enter", "A:exit"],
    )


def test_different_usernames_do_not_block_each_other():
    from src.marzban_lock import PerUsernameLock

    async def scenario():
        locks = PerUsernameLock()
        events = []

        async def worker(username, name, delay):
            lock = locks.get(username)
            async with lock:
                events.append(f"{name}:enter")
                await asyncio.sleep(delay)
                events.append(f"{name}:exit")

        # A holds its lock for longer than B needs — if usernames were
        # (wrongly) serialized against each other, B could not start+finish
        # while A is still inside its critical section.
        await asyncio.gather(
            worker("alice", "A", 0.05),
            worker("bob", "B", 0.0),
        )
        return events

    events = asyncio.run(scenario())
    # B (different username, no delay) must be able to fully complete
    # BEFORE A (longer delay) exits — proof of no unnecessary serialization.
    assert events.index("B:exit") < events.index("A:exit")


def test_get_returns_same_lock_object_for_same_username():
    from src.marzban_lock import PerUsernameLock

    locks = PerUsernameLock()
    assert locks.get("alice") is locks.get("alice")
    assert locks.get("alice") is not locks.get("bob")


def test_module_level_singleton_exists():
    from src.marzban_lock import marzban_user_locks
    assert marzban_user_locks.get("x") is marzban_user_locks.get("x")
