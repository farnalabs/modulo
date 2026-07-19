"""Architecture test: no sensitive data in log output.

Only flags cases where a variable named token/secret/password/credential
is directly interpolated into a log format string (not log event names
or extra dict keys that are just identifiers).
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "modulo"

SENSITIVE_VARS = {
    "token",
    "secret",
    "password",
    "credential",
    "api_key",
    "bearer",
    "access_token",
    "refresh_token",
    "private_key",
    "fernet_key",
    "auth_header",
    "auth_token",
}


def test_no_sensitive_data_in_logs():
    violations = []
    for path in SRC.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in ("logger", "log", "_log", "_logger")
                and func.attr in ("debug", "info", "warning", "error", "critical", "exception")
            ):
                continue
            # Check positional args for f-string interpolation of sensitive vars
            for arg in node.args:
                _check_expr_for_sensitive_var(arg, path, node.lineno, violations)
            # Check keyword values (not keys) for sensitive var interpolation
            for kw in node.keywords:
                _check_expr_for_sensitive_var(kw.value, path, node.lineno, violations)
    assert not violations, (
        f"Found {len(violations)} potential sensitive data leak(s).\n"
        "Strip auth tokens, secrets, and credentials before logging.\n" + "\n".join(violations)
    )


def _check_expr_for_sensitive_var(expr, path, lineno, violations):
    """Check if an expression references a sensitive variable name."""
    sensitive_names = SENSITIVE_VARS
    # Look for ast.Name references
    for sub in ast.walk(expr):
        # Direct variable reference (e.g., logger.info(token))
        if isinstance(sub, ast.Name) and sub.id in sensitive_names:
            violations.append(
                f"  {path.relative_to(SRC.parent.parent)}:{lineno}  Variable '{sub.id}' logged — "
                "may contain sensitive data"
            )
            return
        # f-string interpolation with sensitive content
        if isinstance(sub, ast.JoinedStr):
            for value in sub.values:
                if (
                    isinstance(value, ast.FormattedValue)
                    and isinstance(value.value, ast.Name)
                    and value.value.id in sensitive_names
                ):
                    violations.append(
                        f"  {path.relative_to(SRC.parent.parent)}:{lineno}  f-string interpolates "
                        f"'{value.value.id}' into log — sensitive"
                    )
                    return
