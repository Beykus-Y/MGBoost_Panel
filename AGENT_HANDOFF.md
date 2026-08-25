# AGENT_HANDOFF — PH4-02 (crash-safe, update after every major step)

Updated: 2026-08-25, after production deploy completed.

## HEAD / git status

- Local HEAD = production HEAD = `e7d853daa648f2d891d71a6b0956106769a05b3f`
- Pushed to `origin/main`. Working tree clean except pre-existing untracked
  `extra_configs.json` (not ours, ignore).

## PH4-02 goal

Durable migration state machine on top of PH4-01's legacy bridge:
`LEGACY -> MIGRATING -> MIGRATED -> LEGACY_REVOKE_PENDING -> LEGACY_REVOKED`,
plus `ERROR_RECONCILE`. Full spec lives in `ROADMAP.md` PH4-02 entry (now
`[x]`) and `docs/PHASE4_MIGRATION_STATE_MACHINE.md`.

## Status: PH4-02 COMPLETE, verdict `[x]`

Everything in the task is done. Nothing is left to do for PH4-02 itself.

### Implemented

- `src/migration_lifecycle_schema.py` — additive schema, `mgboost_migration_bindings`
  + `mgboost_migration_binding_events`, wired into `src/database.py`
  (`self.migration_lifecycle`).
- `src/migration_lifecycle.py` — `MigrationLifecycleStore`, explicit
  transition allowlist, `reconcile_binding()`, `process_migration_bridge_request()`
  (wraps PH4-01's `resolve_legacy_bridge()` unchanged, no second resolver).
- Dormant: no route/worker calls `process_migration_bridge_request` in
  production. Zero live wiring.

### Tests — all PASS

- `tests/test_migration_lifecycle.py`: 22 passed.
- Full regression: `820 passed, 3 skipped` (baseline before PH4-02 was
  771 passed — zero regressions).

### Real isolated Marzban 0.8.4 gate — PASS

- `scripts/verify_ph4_02_migration_lifecycle_staging.py`, digest
  `gozargah/marzban@sha256:8e422c21997e5d2e3fa231eeff73c0a19193c20fc02fa4958e9368abb9623b8d`.
- Must run the container with `--network host` (this image binds uvicorn to
  loopback-only without TLS; bridged docker networking cannot reach it —
  discovered this session, not previously documented elsewhere).
- 23/23 checks PASS: forward `MIGRATING->MIGRATED` lifecycle, crash/lost-ACK
  convergence (real connection close + fresh `Database()` reopen), and on a
  separate synthetic account a real `MIGRATED->LEGACY_REVOKE_PENDING->
  LEGACY_REVOKED` with an actual legacy-user disable+UUID-rotate, refused
  rollback, child still works after revoke.
- Staging container was torn down after the gate (`docker rm -f
  mgboost-ph4-02-stage`) — nothing left running.

### Production deploy — DONE

- Encrypted backup + restore verification: PASS (`mgboost-secure-backup.service`).
- Pre-deploy cardinality snapshot taken, matched exactly post-deploy.
- `git fetch && git merge --ff-only` on production: fast-forwarded
  `e450656 -> e7d853d` cleanly.
- `systemctl restart mgboost-panel` (schema applies at `Database()` init) —
  all three services (`mgboost-panel`, `mgboost-marzban-broker`,
  `mgboost-child-worker`) active, panel HTTP 200.
- Post-deploy: `mgboost_migration_bindings`=0, `mgboost_migration_binding_events`=0,
  `LEGACY_BRIDGE_ENABLED`/`OPAQUE_SUBSCRIPTION_ENABLED`=False,
  `PH3_04_ENFORCEMENT_MODE`=OFF, all other table cardinalities
  (accounts=2, aliases=4, slots=3, generations=5, child_intents=5,
  telegram_identities=1, bridge_bindings=0) identical pre/post.
  `PRAGMA quick_check=ok`, 0 FK violations.

### Docs/ROADMAP/CHANGELOG — DONE

- `ROADMAP.md` PH4-02 → `[x]` with full evidence block.
- `CHANGELOG.md` Unreleased/Security has the PH4-02 entry.
- `docs/PHASE4_MIGRATION_STATE_MACHINE.md` written.
- Commit `e7d853d` on `main`, pushed to `origin/main`.

## Known non-blocking backlog (did not block PH4-02, not fixed)

- None newly discovered this session that were in scope to note — no
  correctness/security/credential/data/production-safety residuals were
  found during PH4-02 implementation or gates.

## Explicitly NOT started (per owner instruction — do not start without new permission)

PH4-03 (real canary migration), PH4-04 (opaque URL rollout), PH4-05 (grace),
PH4-06 (production legacy revoke), mass migration, PH5+.

## Next step if resumed

PH4-02 is closed. Wait for explicit owner instruction before starting
PH4-03. If asked to continue the roadmap critical path, re-read `ROADMAP.md`
PH4-03's entry (`Depends: PH3-06/09, PH4-01/02`) and confirm no source-of-truth
contradiction before beginning design work — same discipline as every prior
phase in this project.
