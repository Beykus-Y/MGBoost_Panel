# Shadowsocks retirement — VLESS-only product contract

Date: 2026-08-25

## Product and activation boundary

DL-046 retires Shadowsocks completely from MGBoost. Existing Shadowsocks proxy
objects are non-functional legacy metadata because production has zero
Shadowsocks inbound and all 25 real subscription responses contain only VLESS
entries. New child users and future account/device flows are VLESS-only. No
Shadowsocks topology or Marzban upgrade for Shadowsocks may be introduced
implicitly.

The cleanup does not rotate/reissue any credential and does not activate a
parent, slot, child, resolver or fail-closed HWID flow.

## Read-only production inventory

The pre-mutation inventory contains 25 Marzban users. Seven have retired
Shadowsocks proxy metadata:

```text
beykus
beykusios
MegochelPC
MegochelAndroid
BeykusLaptop
German
client_buy_5
```

For every user the inventory records the exact username, VLESS UUID SHA-256
verifier/mask, expiry, status, data limit, flow, exact VLESS inbound list,
protocol-only proxy shape and hash-only state/config evidence. It does not emit
raw UUID, Shadowsocks password or subscription bearer.

Production topology is 25 VLESS / 0 Shadowsocks inbound. Fetching all 25 real
legacy subscriptions succeeded without error and produced 734 VLESS entries,
zero Shadowsocks entries and zero entries for any other protocol.

## Typed mutation

`maintenance.user.retire_shadowsocks` accepts only:

```json
{
  "username": "validated legacy username",
  "expected_state_digest": "64 lowercase hex"
}
```

Inside the authenticated localhost broker, under a per-user lock, it:

1. fails closed unless the current topology has zero Shadowsocks inbound;
2. rereads the user and matches the inventory digest;
3. derives the existing VLESS UUID and flow server-side;
4. sends only `proxies={vless: current UUID/flow}` to Marzban;
5. rereads and verifies UUID, flow, inbound set, expiry, status, data limit,
   reset strategy, subscription token verifier and normalized VLESS links;
6. returns only masked/hash evidence;
7. returns `UNCHANGED` without mutation on a safe retry.

Caller-controlled UUID, password, proxy map, inbound set, expiry, status or
data limit is impossible. If Marzban unexpectedly changes a repairable
functional field, the broker repairs only those affected VLESS/user fields and
returns failure so rollout stops. An unrepairable token/config drift triggers
immediate stop and the verified encrypted backup gate; it is never accepted as
success.

## Isolated Marzban 0.8.4 evidence

The exact official image digest used by the existing PH3 gate was started on
literal loopback. A synthetic user was first created with VLESS+Shadowsocks,
then the same DB was restarted under production-equivalent topology of exactly
25 VLESS and zero Shadowsocks inbound. The typed broker operation passed:

- first call: `REMOVED`;
- retry: `UNCHANGED`;
- Marzban modify calls: exactly one;
- payload top-level field: only `proxies`;
- VLESS UUID unchanged;
- VLESS inbound count: 25;
- normalized subscription lines: 25 VLESS before and after, exact match;
- raw UUID occurrence in broker result/log: zero;
- Shadowsocks metadata after reread: absent.

Focused tests pass (`42 passed` before the full run); full regression passes
(`523 passed, 3 skipped`).

## Production gates and rollback

Before mutation:

1. capture the complete masked inventory and real subscription digests;
2. create and restore-verify an encrypted MGBoost/Marzban backup;
3. deploy/restart only the typed localhost broker as required;
4. confirm topology remains 25 VLESS / 0 Shadowsocks.

Canary is `beykusios`. After its typed mutation, reread the user and actual
legacy subscription and require every invariant below. Only then process the
remaining six users one by one with a fresh digest and reread after each.

At any mismatch, stop before the next user. The broker attempts a narrow repair
of the affected user's functional VLESS state. If it cannot prove restoration,
do not perform a blind retry or mass restore; use the verified encrypted backup
and a scoped recovery plan while preserving all other users.

Required final invariants:

- VLESS UUID changes: 0;
- legacy subscription URL/token changes: 0;
- VLESS inbound changes: 0;
- flow, expiry, status and data-limit changes: 0;
- HWID/tariff changes: 0;
- forced client reconfiguration: 0;
- Shadowsocks proxies remaining: 0;
- Shadowsocks subscription entries: 0.

After cleanup, rerun the real PH3-03 VLESS-only child create/reread gate. No
production parent/slot/outbox/child mutation is authorized by this runbook.
