"""PH4-04 crash-safe issuance/rotation orchestration over PH2-01's dormant
`SubscriptionCredentialStore`.

This module never persists a raw token and never invents a second
credential store -- it only sequences the existing `prepare()` /
`activate()` / `abandon_pending()` methods around a caller-supplied
`deliver_fn`, so the exact same convergent behavior applies uniformly
whether the delivery channel is a Telegram message, an admin HTTP response,
or an LK response:

    abandon any stale PENDING_DELIVERY (unrecoverable -- a previous
        attempt's plaintext is gone the moment this call started)
    -> prepare() a fresh generation (old credential, if any, stays ACTIVE)
    -> deliver_fn(raw_token) -- if this raises, the fresh generation is
        left PENDING_DELIVERY (harmless; the old credential keeps working)
        for the *next* call to abandon and retry -- never two ACTIVE
        credentials, never a guessed/reconstructed token
    -> only once delivery did not raise: activate() -- atomically makes the
        new generation ACTIVE and revokes the old one in the same
        transaction.

Authorization is the caller's responsibility (this module takes only an
`actor_ref` string for the audit trail) -- exactly the same boundary
`SubscriptionCredentialStore` itself already draws.
"""

from __future__ import annotations

import time


def issue_or_reissue_credential(
    db, *, account_id: int, actor_ref: str, reason: str, idempotency_key: str,
    deliver_fn, now: int | None = None,
) -> dict:
    timestamp = int(time.time()) if now is None else int(now)
    store = db.subscription_credentials
    store.abandon_pending(
        account_id=account_id, actor_ref=actor_ref,
        idempotency_key=f"{idempotency_key}:abandon-stale", now=timestamp,
    )
    prepared = store.prepare(
        account_id=account_id, actor_ref=actor_ref, reason=reason,
        idempotency_key=idempotency_key, now=timestamp,
    )
    deliver_fn(prepared["raw_token"])
    return store.activate(
        credential_id=prepared["id"], account_id=account_id,
        expected_generation=prepared["generation"], actor_ref=actor_ref,
        idempotency_key=f"{idempotency_key}:activate", now=timestamp,
    )
