# PH1-09 — minimum permissions for the MGBoost sensitive nginx log

Date: 2026-08-25. Status: applied to production. Filesystem/nginx/logrotate
configuration only -- no MGBoost application code changed.

## What this is not

This is not a reopening of PH1-06. PH1-06's own contract (redact sensitive
targets in nginx logs, fixed 30-day retention) was never violated: the
`mgboost_sensitive` nginx log format (`/etc/nginx/conf.d/mgboost-sensitive-log.conf`,
predates this task) already replaces every `/sub/` request's target with a
fixed `<redacted-sensitive-target>` placeholder before it is ever written.
`/var/log/nginx/mgboost-sensitive-access.log` never contained a raw legacy
bearer. The gap found during PH4-01's valid-`/sub` production gate was
narrower: the file's *mode* (`0644`, world-readable) was more permissive
than necessary for a file explicitly named "sensitive," even though its
*content* was already safe.

## Root cause

- The file's `0644 root:root` mode came from nginx's own default
  file-creation behavior the one time the file was first created -- not
  from any explicit, intentional permission grant.
- The file had never actually been rotated by logrotate, so the generic
  `/etc/logrotate.d/nginx` stanza's `create 0640 www-data adm` had never
  been applied to it. Even if it had, `adm`-group readability is broader
  than this file needs.
- `logrotate` (the package/binary) is not installed on this host at all --
  a separate, larger, pre-existing gap than the file-mode issue (see
  "Residual risk" below).

## What actually writes/reads the file

- nginx's `location /sub/ { access_log .../mgboost-sensitive-access.log
  mgboost_sensitive; }` (unchanged by this task) is the only writer.
- Empirically confirmed (not assumed) by forcing a real rotation and
  issuing two real production requests: the actively-written file settles
  at owner `www-data:root` (nginx's worker identity), not `root:root` as
  might be naively assumed from "the master opens logs as root." The
  logrotate `create` directive was set to match this observed reality.
- No other service, user or cron job reads this file for any operational
  purpose today.

## Change made

1. `chmod 600` the existing inode immediately (root:root -> still
   root:root, now `0600`).
2. `/etc/logrotate.d/nginx` (the generic Debian nginx package stanza)
   narrowed from the glob `/var/log/nginx/*.log` to an explicit
   `access.log`/`error.log` list -- those two files' own `create 0640
   www-data adm`/30-day retention are completely unchanged. This avoids a
   double-rotation hazard: two glob-based stanzas both matching the same
   file would each independently try to rotate it.
3. New `/etc/logrotate.d/mgboost-sensitive-nginx`, matching only
   `/var/log/nginx/mgboost-sensitive-access.log`:

   ```
   /var/log/nginx/mgboost-sensitive-access.log
   {
       daily
       missingok
       rotate 30
       compress
       delaycompress
       notifempty
       create 0600 www-data root
       sharedscripts
       postrotate
           invoke-rc.d nginx rotate >/dev/null 2>&1
       endscript
   }
   ```

   Retention stays 30 days (DL-042), unchanged. `postrotate` sends `USR1`
   to the nginx master (the exact same mechanism the generic stanza already
   uses), which reopens log files without a restart.

Both `/etc/logrotate.d/nginx` and the new
`/etc/logrotate.d/mgboost-sensitive-nginx` were backed up before editing
(`/root/config-backups/<date>/`). Versioned copies of both deployed files
are tracked at `ops/nginx/logrotate.d-nginx` and
`ops/nginx/logrotate.d-mgboost-sensitive-nginx` in this repository.

## Verification (real production, 2026-08-25)

- `nginx -t`: passed before and after.
- A real forced rotation (file moved aside, a fresh file created with the
  new stanza's exact `create` semantics, nginx signaled via the same
  `invoke-rc.d nginx rotate` the config uses) produced a new inode.
- Two real HTTP requests to `/sub/<garbage-token>` (no real bearer used)
  wrote to the new file; it settled at `www-data:root 0600`. The archived
  `.1` file stayed `root:root 0600`.
- `nobody` (an unrelated local identity) got `Permission denied` reading
  the live file.
- Both the live and rotated files contain **0** occurrences of any raw
  `/sub/{token}`-shaped path -- only the fixed redacted placeholder.
- `nginx`, `mgboost-panel`, `mgboost-marzban-broker`, `mgboost-child-worker`
  stayed active throughout. Legacy `/sub` invalid-token smoke (`404`) and
  admin panel reachability (`200`) were unchanged.

## Residual risk (explicitly out of scope for this task)

`logrotate` is not installed on this host at all -- no scheduled rotation
currently runs for *any* nginx log, not just this one. This predates PH1-09
and is a materially larger change (provisioning a rotation daemon for the
first time) than "narrow an existing file's permissions," so it was not
done here. The logrotate config files added above are correct and ready
for whenever a rotation mechanism is provisioned; the one rotation
performed for this verification was executed manually, replicating the
configured stanza's exact `create`/`postrotate` semantics.
