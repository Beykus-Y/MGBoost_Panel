import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def load_patcher():
    path = ROOT / "scripts" / "patch_marzban_login_report.py"
    spec = importlib.util.spec_from_file_location("patch_marzban_login_report", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


SOURCE = '''
def admin_token(request, form_data, db):
    client_ip = "127.0.0.1"
    dbadmin = validate_admin(db, form_data.username, form_data.password)
    if not dbadmin:
        report.login(form_data.username, form_data.password, client_ip, False)
        raise RuntimeError("denied")
    report.login(form_data.username, "🔒", client_ip, True)
'''


def test_patch_redacts_only_failed_login_report_password():
    module = load_patcher()
    patched, changed = module.patch_source(SOURCE)
    assert changed is True
    assert "validate_admin(db, form_data.username, form_data.password)" in patched
    assert "report.login(form_data.username, form_data.password" not in patched
    assert patched.count('report.login(form_data.username, "🔒"') == 2
    module.validate_source(patched, patched=True)


def test_patch_is_idempotent():
    module = load_patcher()
    patched, _ = module.patch_source(SOURCE)
    repeated, changed = module.patch_source(patched)
    assert changed is False
    assert repeated == patched


def test_patch_file_changes_only_expected_call(tmp_path):
    module = load_patcher()
    target = tmp_path / "admin.py"
    target.write_text(SOURCE, encoding="utf-8")
    assert module.patch_file(target) is True
    assert module.patch_file(target) is False
    module.validate_source(target.read_text(encoding="utf-8"), patched=True)


@pytest.mark.parametrize(
    "source",
    [
        SOURCE.replace("report.login", "report.audit", 1),
        SOURCE.replace(
            "raise RuntimeError",
            "report.login(form_data.username, form_data.password, client_ip, False)\n"
            "        raise RuntimeError",
        ),
        SOURCE.replace("form_data.password, client_ip, False", '"masked", client_ip, False'),
    ],
)
def test_patch_refuses_unknown_or_ambiguous_source(source):
    module = load_patcher()
    with pytest.raises(ValueError):
        module.patch_source(source)
