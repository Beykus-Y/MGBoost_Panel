# PH3-03 durable child worker and reconciliation

Date: 2026-08-25

## Safety boundary

The worker is a separate `mgboost-child-worker.service`. It uses the unprivileged
`mgboost` Unix account and a minimal root-managed environment; it does not load
the panel `.env` and receives no Marzban SUDO credential. All remote reads and
future writes cross the existing HMAC-authenticated localhost broker. There is
no nginx/public route.

Production activation for this rollout is deliberately narrower than the
general worker implementation:

- `CHILD_WORKER_MODE=reconcile_only`;
- the exact allowlist contains only
  `op_lw33pjhqhnvorrgh4p754bnc34`;
- the operation must already be `APPLIED`;
- preflight rejects any pending/retry/in-flight outbox row;
- no resolver, legacy-device scanner or automatic intent creator imports the
  worker.

Startup can therefore create only the additive workflow tracking row/events
for the existing dormant canary and can perform only `child.user.observe`.

## State machine

Provisioning outbox:

```text
PENDING/RETRY -> IN_FLIGHT -> observe
  remote absent -> child.user.ensure -> observe -> APPLIED
  remote match  -------------------------------> APPLIED
  unavailable  -> RETRY (bounded exponential backoff)
  mismatch/ambiguous/corrupt/stale generation -> ERROR + MANUAL_REVIEW
```

Periodic reconciliation:

```text
PENDING -> IN_SYNC
        -> REMOTE_MISSING -> retry -> MANUAL_REVIEW on exhaustion
        -> REMOTE_MISMATCH/REMOTE_AMBIGUOUS -> MANUAL_REVIEW
        -> UNAVAILABLE -> retry -> IN_SYNC after recovery
```

`APPLIED + remote missing` never invokes CREATE. Contract mismatch never
invokes overwrite/delete. A released/revoked or non-current generation fails
before a broker call and can never be automatically reactivated.

## Correctness and retry

- SQLite `BEGIN IMMEDIATE`, immutable operation identity and unique
  account/slot-generation/operation constraints are the correctness boundary;
  process-local locks are not relied on.
- Provisioning and reconciliation use atomic owner/expiry leases. Another
  process can reclaim a stale lease after 30 seconds.
- Stable operation ID and canonical SHA-256 payload digest are revalidated
  before every remote call.
- Retry defaults to eight attempts with exponential 5/10/20/40/80/160/300
  second delays capped at 300 seconds.
- The worker observes before CREATE and rereads after it. Remote success plus a
  lost local ACK therefore converges as `EXISTING` under the same operation.
- Truly incompatible state becomes durable `MANUAL_REVIEW`; no blind rollback,
  delete or credential rotation is performed.

## Privacy-safe monitoring

Every cycle emits bounded operation/account/slot/generation identifiers and a
safe error class. SQLite exposes pending count/oldest age, retries,
reconciliation errors, remote mismatch, broker/Marzban failures, stale leases,
manual-review/exhaustion and desired/observed divergence. Events are append-only.

Raw UUID, subscription bearer, full HWID and SUDO credentials are neither log
fields nor workflow columns. Remote UUID exists in memory only long enough for
constant-time verifier comparison; persistent state keeps the existing verifier
and mask.

Suggested initial alerts: any manual-review/mismatch immediately; pending age
over five minutes; three consecutive unavailable cycles; any stale-lease event;
or desired/observed divergence lasting over five minutes. External alert
delivery belongs to PH8 monitoring, so current visibility is journal plus the
`child_worker_main.py --once --json` status output.

## Staging evidence

The real gate used immutable Marzban 0.8.4 image digest
`sha256:8e422c21997e5d2e3fa231eeff73c0a19193c20fc02fa4958e9368abb9623b8d`,
exact 25 VLESS/0 Shadowsocks topology and an isolated SQLite DB. It passed:

- remote-created/local-ACK-failed recovery;
- worker restart and broker restart;
- stale reconciliation lease takeover;
- broker/Marzban-unavailable fail-safe and recovery;
- final `IN_SYNC`, exact one remote child and one CREATE;
- exact 25 inbound/flow/VLESS-only contract;
- zero raw UUID persistence.

The unit failure matrix additionally covers crash before remote, crash after
local ACK, duplicate delivery, two worker/DB connections, remote absent,
mismatch, ambiguity, corrupt digest, retry exhaustion and stale generation.

## Production preflight, deploy and rollback

1. Capture masked legacy/child/cardinality baselines and run a fresh encrypted
   restore-verified backup.
2. Pull the exact reviewed commit and restart the broker so
   `child.user.observe` is available.
3. Restart the panel once to apply the additive workflow migration; confirm it
   changes no existing runtime row.
4. Install the fixed environment with
   `scripts/configure_ph3_03_worker.py` and install/verify the systemd unit.
5. Run `scripts/preflight_ph3_03_worker_production.py`. It must report
   `READ_RECONCILE_ONLY`, one APPLIED operation, zero pending operations, one
   remote child and exact 25/0 topology.
6. Start the worker and verify the canary becomes `IN_SYNC` through an observe
   only event. Re-run all legacy smoke/invariant checks and raw-secret scans.

Rollback stops/disables only the worker and restores the previous application
commit if needed. The additive tables can remain ignored by the old binary.
Do not delete the remote child or local audit/workflow rows. No legacy or child
credential changes are required for rollback.

If the broker/Marzban is unavailable, reconciliation records `UNAVAILABLE` and
backs off; it cannot report success. The current legacy `/sub` path bypasses the
broker and remains functional. Future child subscription refresh still has no
raw-credential cache and will fail safely during Marzban API outage.

## Production evidence

Production deployed commit `511a117b3663a979e92de0ae493045120214dcd2` after a
fresh root-only encrypted backup passed create and restore verification. The
broker restart had one pre-effect connection refusal during its one-second
listener startup window; the worker was still absent, the listener stabilized,
and both typed observe and legacy list immediately passed before continuing.

The panel restart applied only `ph3_03_child_workflow_v1`; both workflow tables
were empty and all parent/slot/intent/outbox cardinality was unchanged. The
fixed preflight then reported `READ_RECONCILE_ONLY`, zero pending operations,
one matching remote child and exact 25 VLESS/0 Shadowsocks topology before the
worker was enabled.

The first production cycle produced exactly `CHECK_STARTED` and `MATCHED` and
ended `IN_SYNC`: `provisioned=0`, retries/errors/manual-review/divergence=0.
The only additive runtime state is one workflow row plus its append-only read
events. Parent=1, aliases=3, slot/generation=1/1, intent=1, outbox=1 and remote
child=1 remained constant; the outbox remained `APPLIED`, attempt 1, and the
UUID mask remained `uuid_d4ae1519`.

Pre/post aggregate identity/config digests matched for all 26 remote users with
26/26 config fetches and no errors. Legacy device/HWID counts and digests
matched at 71/71. The real legacy MGBoost `/sub` path returned the existing
source UUID in a functional VLESS config; public admin/LK, signed/unsigned
Filin, three Stars tariffs/two already-refunded invoices, panel/broker/worker/
nginx health and SQLite quick/FK checks passed. Raw child UUID/current token and
generic credential-pattern occurrences in DB/application/broker/worker/nginx
logs were zero. The worker process contains no Marzban SUDO, Telegram, panel
secret or Internal API key environment variable.

Production invariants caused by this rollout: legacy UUID changes 0;
subscription URL/token changes 0; HWID changes 0; expiry changes 0; tariff
changes 0; forced client reconfiguration 0; unexpected config changes 0; new
parent/slot/intent/remote-child counts 0. PH3-04, PH3-05, Phase 4 and the
subscription resolver remain untouched.
