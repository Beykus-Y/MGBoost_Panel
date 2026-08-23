# PH1-06 retention, backup and legacy-token containment

This runbook implements DL-042 without rotating or revoking any working
legacy user token. User-token rotation/reissue remains staged Phase 4 work.

## Fixed retention

- sensitive legacy nginx/application/journal evidence: 30 days;
- ordinary operational logs without credentials: no more than 60 days;
- regular encrypted database backups: 90 days;
- exactly one encrypted legacy-token quarantine snapshot: 180 days.

Presence of a credential in an encrypted artifact never replaces rotation.
Cleanup happens only after an isolated restore succeeds and the Phase 4
rotation/reissue strategy is confirmed. Production evidence is not deleted
ad hoc.

## Cutover order

1. Capture masked user/config/UUID/expiry/device digests.
2. Generate separate root-owned `0600` random passphrase files at
   `/etc/mgboost/backup.passphrase` and
   `/etc/mgboost/quarantine.passphrase`; never print them.
3. Run `scripts/create_legacy_quarantine.py` once. Verify the produced
   artifact immediately with `--verify`; keep the encrypted artifact and key
   accessible only to the minimum owner/service identity.
4. Install the application/log-redaction change and nginx sensitive-route log
   format. The `/sub/` and `/lk/` locations must write the fixed redacted
   format, not the inherited combined request/Referer log. HTTP redirect
   virtual hosts must also avoid logging raw request targets.
5. Stop MGBoost, make an online encrypted backup, and verify an isolated
   restore. Run `Database.migrate_legacy_subscription_token_storage()` once,
   verify `PRAGMA quick_check`, then start MGBoost. This replaces local raw
   keys with `sha256:<hex>` references; it does not change a Marzban token.
6. Install and enable `mgboost-secure-backup.service/.timer`. The daily job
   creates both SQLite backups through the SQLite backup API, encrypts the
   private tar with GnuPG AES-256, atomically publishes mode `0600`, performs
   an isolated decrypt/checksum/`PRAGMA quick_check`, and only then applies
   the 90-day retention selector.
7. Configure nginx logrotate/journald so sensitive evidence is retained no
   longer than 30 days. Perform any first deletion only after steps 2–6 pass.
8. Send a secret canary through legacy `/sub` and old LK `?token=` entry,
   then prove the raw canary is absent from new nginx/application/journal
   records, active local token columns and the encrypted backup byte stream.
9. Re-run exact legacy alias and aggregate compatibility gates.

## Rollback

Do not rotate user credentials. Keep redacted nginx/application logging and
encrypted backups in place. Application rollback remains functionally
compatible with hashed `user_devices`/`sub_requests` keys (the device identity
is `username + request_key`, not the token column). If a legacy Hysteria
counter requires plaintext-key compatibility, restore the verified encrypted
pre-migration database during a controlled downtime; never guess or reverse a
hash. Compare the frozen user/config/device snapshot after recovery.

## Operational checks

```bash
python3 scripts/secure_db_backup.py --retention-dry-run
python3 scripts/secure_db_backup.py --verify /var/backups/mgboost/<artifact>.tar.gpg
systemctl list-timers mgboost-secure-backup.timer
```

Do not put artifact paths containing tokens, passphrases, decrypted contents,
or raw canary values in tickets, chat, git, journal or monitoring labels.
