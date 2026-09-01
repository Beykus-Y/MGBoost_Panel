#!/usr/bin/env python3
"""Single durable driver for due legacy->commercial transitions."""
from __future__ import annotations
import argparse, json, socket, time

def main() -> None:
    parser=argparse.ArgumentParser(); parser.add_argument('--db',required=True); parser.add_argument('--now',type=int,default=None); args=parser.parse_args()
    import src.database as database_module
    from src.legacy_commercial_transition_worker import run_worker_tick
    from src.service_marzban import ServiceMarzbanClient
    database_module.DB_PATH=args.db; db=database_module.Database(); now=int(time.time()) if args.now is None else int(args.now)
    clock=(lambda: int(time.time())) if args.now is None else (lambda: now)
    service=ServiceMarzbanClient()
    def sync_fn(payload):
        outcome=service.sync_child_user_state(payload)
        if not isinstance(outcome,dict) or not isinstance(outcome.get('outcome'),str):
            raise RuntimeError('InvalidParentSyncOutcome')
        return outcome
    try:
        result=run_worker_tick(
            db, sync_fn=sync_fn, revoke_fn=service.revoke_child_user,
            now=now, clock=clock, worker_prefix=socket.gethostname(),
        )
    finally: db._conn.close()
    print(json.dumps(result,sort_keys=True))
if __name__=='__main__': main()
