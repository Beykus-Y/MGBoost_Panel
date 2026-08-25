"""PH1-10 -- durable regression guard for the deployed nginx logrotate
configuration, checked against the versioned reference copies in
`ops/nginx/`. This cannot exercise the real production scheduler (that is
proven once, operationally, in the PH1-10 gate itself -- see
`docs/PHASE1_SENSITIVE_LOG_PERMISSIONS.md`), but it durably guards the
config *content* this repository ships and expects to be deployed: exact
30-day retention (DL-042), the sensitive log's minimal `create` mode, no
overlapping glob between the two stanzas (the double-rotation hazard PH1-09
specifically avoided), and the postrotate reopen hook."""

import os
import re

_OPS_DIR = os.path.join(os.path.dirname(__file__), "..", "ops", "nginx")


def _read(name):
    with open(os.path.join(_OPS_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_sensitive_stanza_matches_only_the_one_sensitive_log():
    text = _read("logrotate.d-mgboost-sensitive-nginx")
    header = text.split("{", 1)[0]
    paths = [line.strip() for line in header.strip().splitlines() if line.strip()]
    assert paths == ["/var/log/nginx/mgboost-sensitive-access.log"]


def test_sensitive_stanza_retention_is_exactly_30_days_dl_042():
    text = _read("logrotate.d-mgboost-sensitive-nginx")
    assert re.search(r"^\s*daily\s*$", text, re.MULTILINE), "must rotate daily for `rotate 30` == ~30 days"
    match = re.search(r"^\s*rotate\s+(\d+)\s*$", text, re.MULTILINE)
    assert match and int(match.group(1)) == 30


def test_sensitive_stanza_create_mode_is_minimal():
    text = _read("logrotate.d-mgboost-sensitive-nginx")
    match = re.search(r"^\s*create\s+(\S+)\s+(\S+)\s+(\S+)\s*$", text, re.MULTILINE)
    assert match, "sensitive log stanza must set an explicit create mode/owner/group"
    mode, owner, group = match.groups()
    assert mode == "0600", "must never be group/world readable"
    assert owner == "www-data"  # the identity that actually holds the write fd, empirically confirmed
    assert group == "root"


def test_sensitive_stanza_has_postrotate_reopen():
    text = _read("logrotate.d-mgboost-sensitive-nginx")
    assert "postrotate" in text
    assert "invoke-rc.d nginx rotate" in text  # sends USR1 to the nginx master


def test_generic_nginx_stanza_never_globs_the_sensitive_log():
    """The exact hazard PH1-09 fixed: a glob-based generic stanza and the
    dedicated sensitive stanza must never both match the same file, or
    logrotate will rotate it twice per run."""
    text = _read("logrotate.d-nginx")
    header = text.split("{", 1)[0]
    paths = [line.strip() for line in header.strip().splitlines() if line.strip()]
    assert "*" not in "".join(paths), "no glob allowed -- must be an explicit file list"
    assert "/var/log/nginx/mgboost-sensitive-access.log" not in paths
    assert paths == ["/var/log/nginx/access.log", "/var/log/nginx/error.log"]


def test_generic_nginx_stanza_retention_also_unchanged_at_30_days():
    text = _read("logrotate.d-nginx")
    match = re.search(r"^\s*rotate\s+(\d+)\s*$", text, re.MULTILINE)
    assert match and int(match.group(1)) == 30
