# Reviewed legacy expiry correction

`ensure_legacy_paid_compat_entitlement` refuses ACTIVE commercial evidence
without an expiry (`AmbiguousLegacyExpiry`). PRIMARY aliases remain immutable
historical evidence. No enrollment, engine, recovery, or device-limit contract
changes are needed.

The primary-admin capability-gated `resolve_legacy_expiry_ambiguity` in
`src/legacy_paid_compat.py` accepts only a reviewed, active DIRECT account with
owner-attested payment, ambiguous PRIMARY evidence, and a latest ACTIVE/NULL
non-billed commercial legacy-compat subscription.

The owner must supply `account_id`, `capability`, `decision_ref`, `resolution`,
and structured `evidence={"review_ref": "owner-review-123", "owner_confirmed": True}`.
References identify an external reviewed decision; never put UUIDs, HWIDs,
tokens, or credentials into references. Other evidence fields are rejected.

- `FINITE_EXPIRY` requires `expiry` as an exact integer Unix timestamp. Dates
  at or before the operation time produce EXPIRED; future dates produce ACTIVE.
- `NON_EXPIRING` requires explicit confirmation of a historically non-expiring
  subscription and NULL expiry. It produces UNLIMITED. Device-limit exemption
  is unrelated and provides no evidence for this decision.

The existing immutable `mgboost_entitlement_mutations` ledger stores the actor,
decision reference, resolution/evidence, PRIMARY alias ID, safe before/after,
and timestamp. This record is the durable correction, superseding the original
expiry evidence for the pinned subscription. A CAS update and the ledger INSERT
share one BEGIN IMMEDIATE transaction. Audit failure rolls both back.

Identical retries return the same result. Changed decisions and stale subscription
versions fail closed. Repeated ensure uses the correction, including an EXPIRED
result, without creating another subscription. Subsequent admin changes require
their own domain path; ensure refuses to overwrite them.

Read-only verification uses `detect_legacy_expiry_ambiguities(connection)` with
an existing SQLite connection (a SQLite `mode=ro` connection is sufficient).
It returns only account_id, subscription_id, plan_code, and violation_class.
Do not initialize the application Database merely to run a read-only detector,
because application initialization applies schema migrations.

Deployment and actual data correction are separate operations. For each existing
violation the owner must review evidence and explicitly choose a resolution.
After correction, verify canonical entitlement and the existing delivery policy
gate before separately authorizing recovery. No WL override is involved.
