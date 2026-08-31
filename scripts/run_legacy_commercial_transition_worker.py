#!/usr/bin/env python3
"""Single durable driver for due legacy->commercial transitions."""
from __future__ import annotations
import argparse, json, os, socket, time

def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument('--db',required=True); parser.add_argument('--now',type=int,default=None); args=parser.parse_args()
    import src.database as database_module
    from src.child_lifecycle import process_free, process_revoke
    from src.legacy_commercial_transition import LegacyCommercialTransitionLeaseLost
    from src.parent_sync import run_account_sync_cycle
    from src.service_marzban import ServiceMarzbanClient
    database_module.DB_PATH=args.db; db=database_module.Database(); now=int(time.time()) if args.now is None else int(args.now)
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
        worker_id=f"legacy-transition:{socket.gethostname()}:{os.getpid()}"
        for transition in db.legacy_commercial_transitions.claim_due(worker_id=worker_id,now=now):
            try:
                db.legacy_commercial_transitions.validate_due_source(transition['id'])
                # Each selected immutable generation is re-read immediately
                # before its canonical REVOKE -> verify -> FREE sequence.
                selected=db._conn.execute(
                    "SELECT s.slot_generation_id,g.account_id AS generation_account_id,"
                    "g.status AS generation_status,ds.current_generation,g.generation,"
                    "c.id AS child_id,c.account_id AS child_account_id,c.observed_state "
                    "FROM mgboost_legacy_commercial_transition_selections s "
                    "JOIN mgboost_device_slot_generations g ON g.id=s.slot_generation_id "
                    "JOIN mgboost_device_slots ds ON ds.id=g.slot_id "
                    "LEFT JOIN mgboost_child_user_intents c ON c.slot_generation_id=g.id "
                    "WHERE s.transition_id=?", (transition['id'],),
                ).fetchall()
                if selected:
                    for item in selected:
                        current=(item['generation_account_id']==transition['account_id']
                                 and item['child_account_id']==transition['account_id']
                                 and item['generation_status']=='ACTIVE'
                                 and item['current_generation']==item['generation']
                                 and item['child_id'] is not None
                                 and item['observed_state']=='ACTIVE')
                        if not current:
                            proven=db._conn.execute(
                                "SELECT operation_kind,state FROM mgboost_child_lifecycle_operations "
                                "WHERE account_id=? AND old_slot_generation_id=? "
                                "AND operation_kind IN ('REVOKE','FREE')",
                                (transition['account_id'],item['slot_generation_id']),
                            ).fetchall()
                            if {tuple(value) for value in proven}!={('REVOKE','APPLIED'),('FREE','APPLIED')}:
                                raise RuntimeError('SelectedGenerationStale')
                            continue
                        revoke=db.child_lifecycle.prepare_revoke(account_id=transition['account_id'],old_child_intent_id=item['child_id'],reason='legacy commercial selected retirement',idempotency_key=f"legacy-transition-revoke:{transition['id']}:{item['slot_generation_id']}",now=now)
                        process_revoke(db,revoke['operation_id'],worker_id='legacy-transition-worker',revoke_fn=service.revoke_child_user,now=now)
                        free=db.child_lifecycle.prepare_free(account_id=transition['account_id'],old_child_intent_id=item['child_id'],reason='legacy commercial selected retirement',idempotency_key=f"legacy-transition-free:{transition['id']}:{item['slot_generation_id']}",now=now)
                        process_free(db,free['operation_id'],worker_id='legacy-transition-worker',now=now,strict_generation=True); result['retired']+=1
                assessed=db.legacy_commercial_transitions.assess_capacity(
                    transition['id'],now=now,worker_id=worker_id); result['assessed']+=1
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
