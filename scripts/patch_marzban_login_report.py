#!/usr/bin/env python3
"""Redact the password argument in Marzban 0.8.4 failed-login reports.

This is deliberately a narrow, fail-closed source patch for the immutable
Marzban image used in Phase 1.  A changed upstream call shape must stop the
image build instead of silently leaving plaintext password reporting enabled.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path


REDACTION = "🔒"
ORIGINAL_CALL = (
    "report.login(form_data.username, form_data.password, client_ip, False)"
)
PATCHED_CALL = f'report.login(form_data.username, "{REDACTION}", client_ip, False)'


def _is_report_login(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "login"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "report"
        and len(node.args) == 4
        and not node.keywords
    )


def _report_calls(source: str) -> list[ast.Call]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("Marzban admin router is not valid Python") from exc
    return [node for node in ast.walk(tree) if _is_report_login(node)]


def _is_bool_argument(node: ast.AST, expected: bool) -> bool:
    return isinstance(node, ast.Constant) and node.value is expected


def _is_password_argument(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "password"
        and isinstance(node.value, ast.Name)
        and node.value.id == "form_data"
    )


def _is_redaction_argument(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == REDACTION


def validate_source(source: str, *, patched: bool) -> None:
    calls = _report_calls(source)
    failed = [call for call in calls if _is_bool_argument(call.args[3], False)]
    succeeded = [call for call in calls if _is_bool_argument(call.args[3], True)]
    if len(calls) != 2 or len(failed) != 1 or len(succeeded) != 1:
        raise ValueError("Unexpected Marzban report.login call shape")

    expected = _is_redaction_argument if patched else _is_password_argument
    if not expected(failed[0].args[1]):
        raise ValueError("Unexpected failed-login report password argument")
    if not _is_redaction_argument(succeeded[0].args[1]):
        raise ValueError("Unexpected successful-login report password argument")


def patch_source(source: str) -> tuple[str, bool]:
    original_count = source.count(ORIGINAL_CALL)
    patched_count = source.count(PATCHED_CALL)

    if original_count == 0 and patched_count == 1:
        validate_source(source, patched=True)
        return source, False
    if original_count != 1 or patched_count != 0:
        raise ValueError("Expected exactly one unpatched failed-login report call")

    validate_source(source, patched=False)
    result = source.replace(ORIGINAL_CALL, PATCHED_CALL, 1)
    validate_source(result, patched=True)
    return result, True


def patch_file(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    patched, changed = patch_source(source)
    if changed:
        path.write_text(patched, encoding="utf-8")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    changed = patch_file(args.path)
    print("marzban_login_report_patch=" + ("APPLIED" if changed else "ALREADY_APPLIED"))


if __name__ == "__main__":
    main()
