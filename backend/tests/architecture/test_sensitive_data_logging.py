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
        except (SyntaxError, UnicodeDecodeError, OSError):
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


#: Max characters of a sensitive value that a truncated prefix log may expose
#: (e.g. ``token[:10]``) before it is treated as a leak. Truncated prefixes are
#: a deliberate, well-known diagnostics idiom (e.g. JWT header bytes) and carry
#: no usable secret material.
_SAFE_PREFIX_CHARS = 12


def _safe_truncated_prefix_names(expr: ast.expr) -> set[ast.Name]:
    """Return Name nodes in ``expr`` used only as a small leading slice or single-char access.

    A truncated-prefix log (``token[:10] + "..."``) intentionally exposes only
    the first few characters of a value and is not a leak. A bare constant
    index (e.g. ``token[3]``) exposes at most one character and is treated the
    same way. Only the specific Name node used as the base of such a subscript
    is safe; any other reference to the same variable in the expression (full
    value, long slice, mid-string slice) is still flagged.
    """
    safe: set[ast.Name] = set()
    for sub in ast.walk(expr):
        if not isinstance(sub, ast.Subscript):
            continue
        if not isinstance(sub.value, ast.Name):
            continue
        lower = upper = None
        if isinstance(sub.slice, ast.Slice):
            lower, upper = sub.slice.lower, sub.slice.upper
        elif isinstance(sub.slice, ast.Constant):
            upper = sub.slice
        if lower is not None and not (isinstance(lower, ast.Constant) and lower.value == 0):
            continue
        if not (
            upper is not None
            and isinstance(upper, ast.Constant)
            and isinstance(upper.value, int)
            and 0 <= upper.value <= _SAFE_PREFIX_CHARS
        ):
            continue
        safe.add(sub.value)
    return safe


def _check_expr_for_sensitive_var(expr, path, lineno, violations):
    """Check if an expression references a sensitive variable name."""
    # Only the specific Name nodes used as a truncated-prefix slice base are safe
    safe_names = _safe_truncated_prefix_names(expr)
    # Look for ast.Name references
    for sub in ast.walk(expr):
        # Direct variable reference (e.g., logger.info(token))
        if isinstance(sub, ast.Name) and sub.id in SENSITIVE_VARS:
            if sub in safe_names:
                continue
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
                    and value.value.id in SENSITIVE_VARS
                ):
                    violations.append(
                        f"  {path.relative_to(SRC.parent.parent)}:{lineno}  f-string interpolates "
                        f"'{value.value.id}' into log — sensitive"
                    )
                    return
