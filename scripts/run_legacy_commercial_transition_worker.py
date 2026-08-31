#!/usr/bin/env python3
"""Single durable driver for due legacy->commercial transitions."""
from __future__ import annotations
import argparse, json, os, secrets, socket, time

def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument('--db',required=True); parser.add_argument('--now',type=int,default=None); args=parser.parse_args()
    import src.database as database_module
    from src.child_lifecycle import process_free, process_revoke
    from src.legacy_commercial_transition import LegacyCommercialTransitionLeaseLost
    from src.parent_sync import run_account_sync_cycle
    from src.service_marzban import ServiceMarzbanClient
    database_module.DB_PATH=args.db; db=database_module.Database(); now=int(time.time()) if args.now is None else int(args.now)
    clock=(lambda: int(time.time())) if args.now is None else (lambda: now)
    service=ServiceMarzbanClient(); result={'assessed':0,'applied':0,'retired':0,'manual_review':0,'errors':[]}
    def sync_fn(payload):
        outcome=service.sync_child_user_state(payload)
        if not isinstance(outcome,dict) or not isinstance(outcome.get('outcome'),str):
            raise RuntimeError('InvalidParentSyncOutcome')
        return outcome
    try:
        # Crash recovery for the small contract gap between the atomic grace
        # commit and the existing ParentSyncStore's independent transaction.
        grace_accounts=db._conn.execute(
            "SELECT DISTINCT account_id FROM mgboost_legacy_commercial_transitions "
            "WHERE payment_confirmed_at IS NOT NULL AND state IN "
            "('SCHEDULED','SELECTION_REQUIRED','SELECTION_RECORDED')"
        ).fetchall()
        for account in grace_accounts:
            try:
                run_account_sync_cycle(db,account['account_id'],sync_fn=sync_fn,
                                       worker_id=f"legacy-grace:{socket.gethostname()}",now=now)
            except Exception as exc:
                result['errors'].append(type(exc).__name__)
        for ready in db.legacy_commercial_transitions.ready_due(now=now):
            try:
                applied=db.legacy_commercial_transitions.apply_ready(ready['id'],now=now); result['applied']+=1
                run_account_sync_cycle(db,applied['account_id'],sync_fn=sync_fn,worker_id=f"legacy-transition:{socket.gethostname()}",now=now)
            except Exception as exc:
                current=db.legacy_commercial_transitions.get(ready['id'])
                if current and current['state']!='APPLIED':
                    db.legacy_commercial_transitions.manual_review(ready['id'],reason=type(exc).__name__,now=now); result['manual_review']+=1
                result['errors'].append(type(exc).__name__)
        worker_id=(f"legacy-transition:{socket.gethostname()}:{os.getpid()}:"
                   f"{secrets.token_hex(8)}")
        for transition in db.legacy_commercial_transitions.claim_due(worker_id=worker_id,now=now):
            try:
                expected_revision=transition['revision']
                fence=lambda: db.legacy_commercial_transitions.assert_lease(
                    transition['id'],worker_id=worker_id,
                    expected_revision=expected_revision,now=clock())
                fence()
                db.legacy_commercial_transitions.validate_due_source(transition['id'])
                selected=db.legacy_commercial_transitions.validate_retirement_topology(
                    transition['id'],worker_id=worker_id,
                    expected_revision=expected_revision,now=clock())
                if selected:
                    for item in selected:
                        fence()
                        db.legacy_commercial_transitions.validate_retirement_topology(
                            transition['id'],worker_id=worker_id,
                            expected_revision=expected_revision,now=clock())
                        child_id=item['selected_child_id']
                        action_now=clock()
                        revoke=db.child_lifecycle.prepare_revoke(account_id=transition['account_id'],old_child_intent_id=child_id,reason='legacy commercial selected retirement',idempotency_key=f"legacy-transition-revoke:{transition['id']}:{item['slot_generation_id']}",now=action_now)
                        if revoke['state']!='APPLIED':
                            process_revoke(db,revoke['operation_id'],worker_id=worker_id,revoke_fn=service.revoke_child_user,now=action_now,fence_fn=fence)
                        fence()
                        db.legacy_commercial_transitions.validate_retirement_topology(
                            transition['id'],worker_id=worker_id,
                            expected_revision=expected_revision,now=clock())
                        action_now=clock()
                        free=db.child_lifecycle.prepare_free(account_id=transition['account_id'],old_child_intent_id=child_id,reason='legacy commercial selected retirement',idempotency_key=f"legacy-transition-free:{transition['id']}:{item['slot_generation_id']}",now=action_now)
                        if free['state']!='APPLIED':
                            process_free(db,free['operation_id'],worker_id=worker_id,now=action_now,strict_generation=True,fence_fn=fence)
                        result['retired']+=1
                fence()
                assessed=db.legacy_commercial_transitions.assess_capacity(
                    transition['id'],now=clock(),worker_id=worker_id,
                    expected_revision=expected_revision); result['assessed']+=1
                if assessed['state']=='READY_TO_APPLY':
                    applied=db.legacy_commercial_transitions.apply_ready(transition['id'],now=now); result['applied']+=1
                    # Durable local entitlement is committed before remote sync.
                    run_account_sync_cycle(db,applied['account_id'],sync_fn=sync_fn,worker_id=f"legacy-transition:{socket.gethostname()}",now=now)
            except Exception as exc:
                current=db.legacy_commercial_transitions.get(transition['id'])
                # A remote parent-sync failure after the atomic paid apply is
                # recoverable through the durable sync job; never roll the
                # transition back or relabel paid entitlement as review.
                if current and current['state']!='APPLIED' and not isinstance(exc,LegacyCommercialTransitionLeaseLost):
                    db.legacy_commercial_transitions.manual_review(
                        transition['id'],reason=type(exc).__name__,now=now)
                    result['manual_review']+=1
                result['errors'].append(type(exc).__name__)
    finally: db._conn.close()
    print(json.dumps(result,sort_keys=True))
if __name__=='__main__': main()
