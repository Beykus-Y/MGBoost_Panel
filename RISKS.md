# RISKS — independent static audit, 2026-09-06

Baseline `98e27fe9e4ea72727dab36436e573d701c83c3b7` (main = cached origin/main).
These are concrete verification/operational gaps, **not confirmed production bugs**.
Production was not contacted and local DBs were not treated as production snapshots.
Confirmed local defects are separate in [BUGS.md](BUGS.md). Likelihood describes trigger
plausibility; it is not a measured production probability. Existing owner decisions
remain binding; recommendations below are not new accepted policy.

Top priority: RISK-001, RISK-003, RISK-012, RISK-004, RISK-002, RISK-005,
RISK-006, RISK-008, RISK-015, RISK-020. The ordered dependency queue is in
[ROADMAP.md](ROADMAP.md#recommended-remaining-execution-sequence--2026-09-06).

# RISK-001 — Production boundaries and cohort outcome remain unverified

Severity: P1
Likelihood: HIGH
Impact: Wrong or premature revoke; missed failed paid transition
Confidence: HIGH for dated boundary; UNKNOWN for current cohort
Related roadmap: PH4-03/05/06/08, DL-062
Affected components: legacy grace; commercial transition

## Why this is a risk

The recorded transition activation has passed; grace is close. Historical state is not a current operational decision.

## Evidence

ROADMAP PH4-05 stores epoch 1788952105 (2026-09-09 11:08:25 UTC); DL-062 last observed a SCHEDULED transition for 2026-09-01 17:00 UTC. Git contains later fixes 1b6599f/f1e8c02, but no fresh runtime outcome was obtained.

## Trigger / conditions

Before grace-cohort revoke or any transition recovery.

## Current controls

Grace schema enforces monotonic extensions; selected-device transition uses revision/lease fences; no revoke performed here.

## Gaps

Current_end_at, exceptions, real migration/telemetry, all-node child viability and APPLIED/MANUAL_REVIEW outcome are UNVERIFIED_PRODUCTION.

## Recommended mitigation

Read fresh masked DB/runtime state using mode=ro, review every exception and post-boundary transition; separate global legacy revoke from per-payment retirement.

## How to verify/close

Dated per-account eligibility and actual remote verification; no unexplained transitions; owner-authorized cohort gate after the actual current_end_at.

# RISK-002 — Two encrypted DB backups do not establish a consistent system restore

Severity: P1
Likelihood: MEDIUM
Impact: Restore may replay payment or child mutations against the wrong remote state
Confidence: HIGH
Related roadmap: PH1-06, PH8-05
Affected components: scripts/secure_db_backup.py; SQLite; Marzban

## Why this is a risk

Two individually valid SQLite files may describe different moments of the same logical transaction.

## Evidence

create_backup iterates DEFAULT_SOURCES sequentially using SQLite backup; verify_backup checks hashes and quick_check. mgboost-secure-backup.timer is daily; no cross-system quiescence or measured RPO/RTO is implemented by these helpers.

## Trigger / conditions

Concurrent remote mutation during backup or restoration after outage/leak.

## Current controls

0600 encrypted artifacts, immediate isolated decrypt/hash/SQLite checks, 90-day retention.

## Gaps

No measured restore of account/payment/outbox/remote UUID consistency, off-host survivability or actual current timer/key availability evidenced.

## Recommended mitigation

Add reconciliation-aware recovery protocol and second-operator disaster drill; capture snapshot times/manifests; decide RPO/RTO before asserting guarantees.

## How to verify/close

Isolated restore with controlled in-flight mutations and no duplicate application; measured recovery and credential-rotation plan.

# RISK-003 — Reconciliation availability depends on independent worker configuration

Severity: P1
Likelihood: HIGH
Impact: Paid/renewed or expired children may remain divergent
Confidence: HIGH for configuration dependency
Related roadmap: PH3-03/08, PH5-05/10, PH8-04/09
Affected components: child_worker_main.py; parent_sync.py; child_worker.py

## Why this is a risk

Implemented reconciliation is not automatically active on deployment.

## Evidence

_parent_sync_scope defaults PARENT_SYNC_RECONCILIATION_MODE to disabled. build_worker requires CHILD_WORKER_ENABLED and ChildProvisioningWorker requires an explicit op_ allowlist. Unit loads /etc/mgboost/child-worker.env, not panel .env. Bootstrap retirement defaults preview.

## Trigger / conditions

Upgrade with missing worker environment, new child not in allowlist, bot unavailable while template jobs wait.

## Current controls

Explicit canary/global scopes, bounded retry, immutable desired revision; tests/test_child_worker_main_reconciliation.py tests disabled/canary/global.

## Gaps

Installed unit/env, allowed operations, mode and post-ACK worker progress are UNVERIFIED_PRODUCTION; general lifecycle RETRY delivery is caller-dependent.

## Recommended mitigation

Inventory all workflow kinds and their actual dispatchers; verify cycle counters and account coverage before relying on unattended repair.

## How to verify/close

Fresh masked effective-config report plus controlled lost-ACK/expiry convergence for each enabled workflow; no unscheduled due rows.

# RISK-004 — Marzban API state is not proof that remote Xray access changed

Severity: P1
Likelihood: MEDIUM
Impact: Cached/direct configurations may keep working or fail despite an API ACK
Confidence: HIGH
Related roadmap: PH0-05, PH3-05, PH6-01/06/09
Affected components: broker_operations.py; wl_topology.py; wl_topology_guard.py

## Why this is a risk

Application code proves a narrow update and API reread, not data-plane convergence at every node.

## Evidence

Broker child state/revoke/WL operations validate UUID and API fields. Topology assertions compare exact node/tag data. docs/PHASE3_CHILD_LIFECYCLE.md and PHASE6_09_WL_FAIL_SAFE.md describe upstream gates; they are not current runtime artifacts.

## Trigger / conditions

Offline node, failed Xray reload, changed inbound/outbound/cascade wiring or Marzban upgrade.

## Current controls

Exact allowlist, versioned manifests, rereads, retry and ERROR_RECONCILE; unknown WL-like tags block mutation.

## Gaps

No local reproduction here of real cached/direct access refusal, node health or cascaded outbound health. API usage omission is converted to zero by _usage_for_node; upstream absence semantics need verification.

## Recommended mitigation

Gate rollout on the pinned Marzban contract plus per-node data-plane checks, distinguishing no usage from unknown/offline.

## How to verify/close

Controlled cached/direct canary on every relevant node before/after revoke/exhaustion, including offline/reconnect and renamed/deleted inbound cases.

# RISK-005 — Collector lease does not fence every delayed observation

Severity: P1
Likelihood: MEDIUM
Impact: Overcount, false reset, corrupted attribution under overlapping collectors
Confidence: HIGH for missing fence; unproven production interleaving
Related roadmap: PH6-03, PH8-02
Affected components: wl_usage_ledger.py; mgboost-wl-usage-collector.service

## Why this is a risk

Lease acquisition alone cannot protect a long network loop after lease expiry.

## Evidence

run_collection_cycle claims a default 300-second lease once; record_sample receives collector_id but does not validate current lease owner/epoch or observation revision. release checks ownership only at the end. A lower delayed cumulative value is treated as reset.

## Trigger / conditions

A slow cycle overlaps an operator invocation after lease expiry; SQLite serialization cannot make an old remote observation fresh.

## Current controls

BEGIN IMMEDIATE, single-row lease, cursor row_version and immutable sample events.

## Gaps

No lease renewal/per-write fence; tests/test_wl_usage_ledger.py simulates a stale cursor via direct SQL, not a delayed pair of independent collectors. Separate confirmed reset-key defect is BUG-004.

**2026-09-06 factual note:** BUG-004 itself is now `FIXED_IN_MAIN` (see `BUGS.md`). This did not reduce RISK-005: the reset-generation fix disambiguates replay identity across a reset, but adds no lease renewal, per-write fence, or epoch/ordering check against a genuinely delayed or concurrent collector observation. This risk's own gap and "How to verify/close" criteria below remain entirely unaddressed and this risk is not closed.

## Recommended mitigation

Fence observation acquisition/commit by epoch, reject stale ordering, or prove an exclusive scheduler/lock covers all callers.

## How to verify/close

Two independent connections with deterministic lease takeover and delayed network return; no false reset, duplicate charge or stale successful heartbeat.

# RISK-006 — Legacy and canonical mutation authorities still coexist

Severity: P1
Likelihood: HIGH
Impact: Remote entitlement changes can bypass canonical audit and later be overwritten
Confidence: HIGH
Related roadmap: PH2-03, PH4-06/08, PH7-08/11/16
Affected components: routes/internal.py; routes/admin_proxy.py; database.py; bot/LK

## Why this is a risk

Legacy compatibility remains reachable; moving its UI under Technical does not remove its authority.

## Evidence

Internal v1 create/renew/delete remain accepted by default; signed v2 optional. Admin proxy permits primary-admin raw PUT/DELETE/reset with reason. Legacy tg_users/user_devices/hwid_lock and canonical ownership/slots are distinct stores.

## Trigger / conditions

Operator edits a canonical child through Raw Users, Filin retries fresh-nonce add_days, or legacy bearer paths remain after planned revoke.

## Current controls

HMAC durable nonces; optional v2 operation IDs; primary-admin reason gates; canonical reconciliation and explicit legacy invoice_kind.

## Gaps

No proof of Filin v2 adoption; raw mutation does not emit the same account/term/payment authority as canonical writers. Generic API access to child usernames is not automatically a payment fact.

## Recommended mitigation

Map every writer to authority, lock, audit and reconciliation; coordinate external v2 adoption; narrow legacy surface only after compatibility inventory.

## How to verify/close

Caller contract tests and runtime gate for v2 required mutations; cross-path edits must be refused or auditable and reconciled without resurrecting credentials.

# RISK-007 — Secret redaction is incomplete on exception and free-text boundaries

Severity: P1
Likelihood: MEDIUM
Impact: Credentials or customer context can reach journals/provider logs
Confidence: HIGH for uncovered sinks
Related roadmap: PH1-06, PH8-04/06/07
Affected components: broker_server.py; child_worker_main.py; bot_support.py; audit free text

## Why this is a risk

Safe error-class logging at selected call sites does not prove all error paths are safe.

## Evidence

broker_server.py:199 and child_worker_main.py:111/124 use logger.exception; bot support uses exc_info=True in payment durability paths. These emit traceback exception text. tests/test_ops_observability_redaction.py does not cover every worker exception source.

## Trigger / conditions

Exception containing a raw upstream URL/body, user-supplied support message, or operator pasting a credential into a reason.

## Current controls

HTTP target redaction, narrow response projections, hash-only credentials and masked identifiers.

## Gaps

No complete exception-sink/outbound canary at current HEAD; no asserted live leak. Existing legacy evidence is intentionally retained.

## Recommended mitigation

Classify allowed exception fields; test worker/broker/provider errors with synthetic secret markers; review free-text redaction without discarding incident evidence.

## How to verify/close

Synthetic markers absent in captured journals/API/provider payloads for both normal and exceptional paths.

# RISK-008 — Payment refunds and compensations do not share a complete entitlement lifecycle

Severity: P1
Likelihood: MEDIUM
Impact: Refunded money, retained access or unresolved paid-but-not-applied records
Confidence: HIGH
Related roadmap: PH5-05/09/10, PH7-08/10/11, PH8-09
Affected components: stars.py; database.py refund state; manual_payment.py

## Why this is a risk

Payment-level refund evidence and entitlement-level compensation are different operations.

## Evidence

Canonical Stars flow preserves money-only refund behavior; manual applied records have no compensation engine. PH7-10 still mentions compensation action in its scope despite no route implementing it. Manual pending jobs are driven primarily by operator routes; parent sync does not reconcile financial state.

## Trigger / conditions

Mistaken target, partial service usage, post-apply refund, bot outage, permanent MANUAL_REVIEW.

## Current controls

Immutable applied records, durable invoice/payment evidence, refund unknown-state reconciliation, no guessed subtraction.

## Gaps

No end-to-end compensation/dispute workflow for existing paid terms; policy on already-used service must not be invented.

**2026-09-06 factual note:** BUG-001 (a specific failure mode under "post-apply refund" -- a record could be CANCELLED by an operator while its entitlement had already, irreversibly, committed, making any later refund/compensation decision start from a false premise) is now `FIXED_IN_MAIN` (see `BUGS.md`): a durable `APPLYING` freeze means `cancel_record` can no longer succeed once the entitlement mutation may already have committed. This narrows one specific trigger for this risk; it does not close it -- there is still no end-to-end compensation/dispute workflow, no refund semantics were added or implied, and "permanent MANUAL_REVIEW" (an unsolvable divergence needing owner resolution) remains exactly as open as before. This risk is not closed.

## Recommended mitigation

Define owner-approved compensation scenarios, then append correlated corrections; keep financial and entitlement evidence distinct.

## How to verify/close

Failure matrix including late capture/refund ACK loss and mixed payments, with exactly one monetary outcome and explicit entitlement outcome.

# RISK-009 — Pinned invoice terms may differ from the default plan version used by renewal

Severity: P1
Likelihood: MEDIUM
Impact: Later catalog version silently changes an already purchased contract
Confidence: HIGH for code mismatch; scenario not executed
Related roadmap: PH5-01/02/05/09
Affected components: stars_purchase.py; manual_payment.py; subscription_renewal.py; plan_catalog.py

## Why this is a risk

Validating a pinned price row is insufficient if the downstream engine resolves the plan again by code.

## Evidence

Stars _validate_snapshot_locked and manual _validate_plan_snapshot_locked verify pinned rows; apply_same_plan_purchase accepts plan_code and calls get_plan_version(plan_code), whose default version is 1 (plan_catalog.py:88). Invoice plan_version_id is not passed to that engine; duration lookup separately selects the latest duration_version.

## Trigger / conditions

A future catalog sells a plan version other than 1, or duration versions change between invoice creation and apply.

## Current controls

Immutable price/plan records and source snapshots; same-plan-code guard.

## Gaps

No reviewed end-to-end old-invoice/new-plan-version proof found; version selection behavior needs deterministic reproduction before classifying a bug.

## Recommended mitigation

Exercise a pinned version-2 invoice and a changed duration version against the default-version renewal path; pass immutable contract identity through apply if divergence is confirmed.

## How to verify/close

Every term/device/WL rule agrees with the paid snapshot across catalog retirement/version rollover for both channels.

# RISK-010 — Support provider boundary accepts unbounded tool arguments and raw conversation text

Severity: P2
Likelihood: HIGH
Impact: Support failures, excessive DB work or unnecessary external data disclosure
Confidence: HIGH
Related roadmap: PH8-06
Affected components: bot_support.py execute_tool/build_ai_messages/ask_openrouter_with_tools

## Why this is a risk

External model output is untrusted input; support text can itself contain subscription URLs or personal data.

## Evidence

get_ticket_history converts arbitrary limit with int and forwards it to SQL; get_tools provides no minimum/maximum; decoded tool arguments are not required to be an object. build_ai_messages forwards message text/history without credential redaction.

## Trigger / conditions

Negative/huge/malformed limit, malformed tool_calls, user pastes a bearer, model prompt injection.

## Current controls

Tools are explicitly allowlisted, returned tickets filtered by Telegram identity, max_tool_rounds=3, HTTP timeout=30s.

## Gaps

Provider retention/minimisation policy and outbound redaction proof absent; BUG-005 confirms only wrong pagination, not a privacy incident.

## Recommended mitigation

Bound and type-check every tool argument; minimise/redact outbound text and document provider policy; preserve useful support context locally under retention.

## How to verify/close

Captured outbound synthetic payloads prove no raw credentials/cross-user data; malformed tools return controlled responses without unbounded queries.

# RISK-011 — Single-thread HTTP and shared SQLite remain availability bottlenecks

Severity: P1
Likelihood: HIGH
Impact: One slow request or DB writer delays unrelated customers/admin
Confidence: HIGH
Related roadmap: PH0-07, PH8-01/02
Affected components: server.py; database.py; service_marzban.py

## Why this is a risk

Current architecture serializes all HTTP handlers and shares a SQLite connection with bot-side work.

## Evidence

_ServerWithDB inherits HTTPServer; _Handler timeout=15 is a socket timeout, not a whole-operation deadline. Database uses check_same_thread=False and RLock; services use the same data path.

## Trigger / conditions

Slow broker/network call, prolonged BEGIN IMMEDIATE, admin large read, accumulated work during outage.

## Current controls

Body/rate limits and socket timeout; process locks; bounded worker retries.

## Gaps

No current load/soak/multiprocess proof; increasing HTTP workers without shared-session/rate/lock readiness is unsafe.

## Recommended mitigation

Observe real latency/contention and cap work; complete PH8-02 before PH8-01; use explicit deadlines/backpressure and graceful drain.

## How to verify/close

Later authorised bounded slow-upstream/concurrency tests prove unrelated clients make progress, no lost updates, graceful restart.

# RISK-012 — Health read models are not an alerting or heartbeat system

Severity: P1
Likelihood: HIGH
Impact: Stuck payments/revokes can remain unnoticed
Confidence: HIGH
Related roadmap: PH8-04/09, PH4-07
Affected components: ops_observability.py; parent_sync.py; legacy transition worker

## Why this is a risk

A successful health HTTP response may mean only that source queries succeeded.

## Evidence

GET /admin/ops/health uses _safe_source and returns 200 with OK/DEGRADED. Legacy transition view reads review backlog; it has no durable worker-cycle heartbeat. parent-sync drift observer catches errors and continues.

## Trigger / conditions

Timer disabled, silent failed observer, backlog aging without operator opening the dashboard.

## Current controls

WL cycle/collector timestamps, ERROR_RECONCILE/MANUAL_REVIEW states, per-source UNKNOWN.

## Gaps

No actual alert delivery, escalation, production-derived thresholds, or second-operator drill at HEAD.

## Recommended mitigation

Add a measured heartbeat/backlog/alert path to existing workers; distinguish stale/no-cycle from an empty healthy queue.

## How to verify/close

Injected missed cycles and stuck operations produce one actionable redacted alert and recovery notification with a runbook.

# RISK-013 — Historical documentation and nginx example can misdirect deployment

Severity: P1
Likelihood: HIGH
Impact: Incorrect route exposure, broken links or unsafe rollback
Confidence: HIGH
Related roadmap: PH0-02/03/04/06, PH2-04, PH4-04, PH7-12/16, PH8-08
Affected components: ROADMAP; README; AGENT_HANDOFF; nginx.conf.example

## Why this is a risk

Historical paragraphs repeatedly describe old defaults as current. The allowed audit diff cannot fix all other docs/configs.

## Evidence

Current index loads assets/admin/app/router.js, not removed admin.js. nginx.conf.example redirects browser /sub requests to /sub-browser without a token and has no opaque-root location. Current-state baseline says root service/no accounts while unit and bootstrap prove otherwise.

## Trigger / conditions

Fresh deployment from example, source archaeology during incident, interpreting dormant/staging evidence as live.

## Current controls

Dated reconciliation in this ROADMAP; individual phase runbooks and git history retained.

## Gaps

No versioned effective deploy manifest; current production nginx/systemd/files are UNVERIFIED_PRODUCTION. Example is not proof that live traffic is broken.

## Recommended mitigation

Publish a tested complete deployment manifest and reconcile README/handoff/runbooks in a later authorised doc/config task.

## How to verify/close

Build a disposable deployment from repo-only instructions; root opaque, legacy browser, admin and internal routes obey their intended boundaries.

# RISK-014 — Retention automation is incomplete across telemetry and journals

Severity: P2
Likelihood: MEDIUM
Impact: Privacy retention drift and unbounded local growth
Confidence: HIGH for missing repo scheduler
Related roadmap: PH1-06/10, PH4-07, PH8-04/05/06
Affected components: legacy_grace_activity; device_telemetry; ops/nginx; cleanup scripts

## Why this is a risk

Availability of a cleanup function does not make retention automatic.

## Evidence

scripts/cleanup_ph4_05_grace_activity_telemetry.py exists with no matching repo timer; compatibility cleanup has a timer. ROADMAP PH1-10 notes journald MaxRetentionSec unset at its historical check. Canonical device telemetry has immutable identity/history guards.

## Trigger / conditions

Long-lived deployment after 30/60/90/180-day retention windows.

## Current controls

Secure nginx logrotate stanzas; daily encrypted backup retention; compatibility cleanup.

## Gaps

Actual installed schedules and journal settings unknown; lifecycle/telemetry retention must account for audit references before deletion.

## Recommended mitigation

Inventory policy per dataset and prove scheduled bounded cleanup after restore/quarantine prerequisites; no deletion in this audit.

## How to verify/close

Dated dry-run counts and service evidence at each window; no removal of active references or required forensic evidence.

# RISK-015 — Per-account provisioning templates remain active infrastructure credentials

Severity: P1
Likelihood: MEDIUM
Impact: Orphan remote resources and potential access if template secrets escape
Confidence: HIGH for active payload; UNKNOWN for data-plane use
Related roadmap: PH5-11/12, PH7-13/15, DL-058
Affected components: commercial_signup.py; account_consolidation.py

## Why this is a risk

Keeping templates hidden from customers is different from proving they cannot work as subscriptions.

## Evidence

ensure_template_for_account creates status=active, expire=0, data_limit=None with real inbounds; close_account does not consult mgboost_provisioning_templates. DL-058 includes an explicit stop condition if template credentials are usable as independent customer access.

## Trigger / conditions

Terminal account close or accidental template URL/UUID disclosure through privileged tooling.

## Current controls

Deterministic template usernames, same-account source hash verification, no template secret in canonical opaque resolver response.

## Gaps

No demonstrated template credential containment across every legacy/admin/support route; no enforced non-usable data-plane template role; cleanup semantics owner-blocked.

## Recommended mitigation

Verify DL-058 stop conditions before wider rollout and ask owner for terminal-template policy; do not redesign shared templates or delete resources here.

## How to verify/close

Controlled isolated contract test proves containment and non-customer usability, or documents a confirmed defect for owner review; audited terminal cleanup after policy selection.

# RISK-016 — Unused package refund relies on delayed ledger finality

Severity: P1
Likelihood: MEDIUM
Impact: A used package could appear unused when its refund is decided
Confidence: HIGH for missing freshness check; scenario not executed
Related roadmap: PH5-03, PH6-03/08/09
Affected components: wl_packages.py refund_unused_package

## Why this is a risk

SQLite atomicity covers current ledger rows, not traffic still in a remote or delayed collector window.

## Evidence

refund_unused_package derives zero consumption under BEGIN IMMEDIATE but never checks usage_freshness or a final observation watermark; collector polls every 10 minutes. The primitive has no live refund route caller at HEAD (tests use it).

## Trigger / conditions

Future refund integration while telemetry is stale or usage arrives after the refund boundary.

## Current controls

Only zero-derived consumption refunds; revoked bucket immutable; no public automatic package refund.

## Gaps

No finality contract for pending remote bytes; lifetime derivation skips revoked buckets, so later usage may be attributed elsewhere.

**2026-09-06 factual note:** BUG-002 (the "sold manual package omitted from enforcement" defect elsewhere in this file/`BUGS.md`) was reclassified `PREMATURELY_REACHABLE_PATH` and fixed by fail-closed-gating *new* package sales (`WL_PACKAGE_SALES_ENABLED`, off by default). This does not reduce or close RISK-016: `refund_unused_package` itself and its missing freshness/finality check are entirely untouched, and this risk was already noted as having "no live refund route caller at HEAD" -- unrelated to whether new sales can occur. This risk is not closed.

## Recommended mitigation

Define operational finality/control before exposing refunds; reconcile delayed usage and preserve immutable refund evidence.

## How to verify/close

Deterministic late-sample/refund interleaving and collector outage; consumed bucket never refunded by stale-zero evidence.

# RISK-017 — Rebind successor still inherits the old provisioning expiry

Severity: P1
Likelihood: MEDIUM
Impact: Replacement device can start with stale expiry and require reconciliation
Confidence: HIGH for code path; end-to-end consequence unverified
Related roadmap: PH3-05/08, PH7-05
Affected components: child_lifecycle.py process_rebind

## Why this is a risk

A provisioning snapshot is immutable historical input, not the latest parent entitlement.

## Evidence

process_rebind at lines 538–542 reads expire from old mgboost_outbox.payload_json and supplies it to the new child ensure. Existing lifecycle fixture builds expire=0; parent_sync later uses current parent targets.

## Trigger / conditions

Parent renewed/shortened after first provision, followed by rebind while reconciliation is disabled/delayed.

## Current controls

Parent-sync current-generation joins and expiry guards; opaque subscription fetch refuses inconsistent remote state.

## Gaps

No executed rebind-after-renewal proof in this audit; full recovery depends on RISK-003 and immutable successor ensure payload.

## Recommended mitigation

Reproduce finite initial expiry → renewal → rebind → actual resolver/worker convergence before designing a fix; never infer a new product expiry.

## How to verify/close

Successor target matches current parent without resurrecting stale access; restart between generation creation/provisioning remains idempotent.

# RISK-018 — Credential delivery is not atomically coupled to activation

Severity: P1
Likelihood: MEDIUM
Impact: User can receive an unusable URL or lose a concurrent issuance
Confidence: HIGH for separate boundaries; defect not reproduced
Related roadmap: PH2-01/05, PH4-04
Affected components: subscription_credential_issuance.py; bot newsub; ownership_rebind.py

## Why this is a risk

Deliver-then-activate preserves old credentials on delivery failure, but successful transport does not guarantee durable activation.

## Evidence

issue_or_reissue_credential performs abandon_pending → prepare → deliver_fn → activate in separate steps; bot has its own analogous sequence. Another issuance can abandon a pending generation; compromise rebind uses a separate credential prepare/activate sequence.

## Trigger / conditions

Concurrent bot/admin/LK issuance, timeout after sending, crash between activate and bookkeeping, repeated compromised recovery.

## Current controls

One ACTIVE credential partial-unique index, generation CAS, terminal revoke, explicit user rotation confirmation and server ownership.

## Gaps

No current full mixed-entrypoint delivery/concurrency proof; one-time raw token is intentionally unrecoverable.

## Recommended mitigation

Exercise per-account concurrent issuers and all crash points; document recoverable user journey and make active pending ownership explicit if needed.

## How to verify/close

No success response with permanently invalid delivered token without an explicit recovery state; no old compromised token resurrection or implicit repeat rotation.

# RISK-019 — Tests exist without a durable current-HEAD continuous execution gate

Severity: P1
Likelihood: HIGH
Impact: Regressions can ship behind historical pass counts
Confidence: HIGH
Related roadmap: PH0-07, PH8-07
Affected components: tests/; git history; repo CI inventory

## Why this is a risk

Happy tests at earlier commits do not establish present cross-phase correctness.

## Evidence

136 tracked test modules; 341 tracked Python files parsed. No tracked .github workflow/pytest config was found. Recent bootstrap runbook explicitly states full suite/browser not run; ROADMAP last changed at 0903d49, before later runtime fixes.

## Trigger / conditions

Cross-domain edits, new migration/worker mode, upgrades or deploy from stale checklist.

## Current controls

Extensive negative tests and targeted crash tests; phase verifier scripts and historical local logs described in docs.

## Gaps

No current complete regression/browser/staging evidence was executed or independently obtained; user explicitly prohibited heavy tests in this session.

## Recommended mitigation

Create explicit CI/deploy evidence tied to commit, environment and migrations; add missing behavioral boundaries from BUGS instead of relying on counts.

## How to verify/close

Later authorised gates produce retained artifacts for exact HEAD; accepted skips/waivers scoped and dated, no automatic claim of green.

# RISK-020 — Additive schema history does not imply a safe code-only rollback

Severity: P1
Likelihood: MEDIUM
Impact: Old code can misread newer rows or undo required security gates
Confidence: HIGH
Related roadmap: PH1-06, PH3-08, PH5-13, PH6-03/09, PH8-03/05/08
Affected components: Database._create_tables; *_schema_v2.py; runbooks

## Why this is a risk

All migrations run during Database initialization; a nominal read-only helper that constructs Database can write schema.

## Evidence

database.py:499–538 wires v1 then v2 migrations, including rebuilt period-aware usage sample table, parent-sync verification, promo snapshot and legacy transition additions. Historical PH6-09 rollback says timer disable plus code revert can never mutate, but old code may omit freshness gates.

## Trigger / conditions

Run an old verifier against current production or revert binaries after new periods/credentials/ledger data exist.

## Current controls

Checksums and schema verification fail closed; immutable history; new read-only preview scripts use mode=ro/query_only.

## Gaps

No universal down-migration or remote-state compensation proof; old immutable rollback paragraphs predate later invariants.

## Recommended mitigation

Keep supported binary/schema compatibility matrix; read production with sqlite mode=ro only; pause writers and reconcile unknown remote outcomes before any rollback.

## How to verify/close

Isolated forward/restart/rollback drill against newer-shaped data preserves payment, credentials, consumed history and fail-closed checks.
