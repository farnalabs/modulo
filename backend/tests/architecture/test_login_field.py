"""Architecture test: login test payloads use 'email', not 'username'.

FastAPI's OAuth2PasswordRequestForm expects 'email' as the username field.
Test fixtures that send 'username' get a silent 422 and cascade failure.
This only flags payloads that appear to be login requests (username+password
combo sent via client.post or similar), not external API mocks nor the
runtime-config ``autoLogin`` block (a config blob the SPA maps onto the login
request's ``email`` field, not a login request itself).
"""

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent.parent / "tests"
# Match dict-like patterns containing both 'username' and 'password' (login payload).
# A bounded window (never crossing a `}`) is used so a multi-line payload is
# caught while two unrelated dicts far apart in the file are not. `re.DOTALL`
# lets the window span newlines inside a single dict literal.
LOGIN_PAYLOAD = re.compile(
    r"""['"]username['"]\s*:\s*[^}]{0,80}['"]password['"]""",
    re.DOTALL,
)
# Login payloads may order 'password' before 'username'; flag both key orders.
LOGIN_PAYLOAD_PASSWORD_FIRST = re.compile(
    r"""['"]password['"]\s*:\s*[^}]{0,80}['"]username['"]""",
    re.DOTALL,
)
# Skip files mocking external APIs (connectors, bitbucket, gitlab, discord, etc.)
EXCLUDE_PATTERNS = ("connector", "bitbucket", "gitlab", "discord", "scanner", "ci_runner")
# A `username`/`password` pair nested directly under `autoLogin` is a rendered
# runtime-config blob, not a login request: the SPA maps `username` onto the
# login request's `email` field (frontend/src/App.vue). Bounded so an
# `autoLogin` in an unrelated earlier dict cannot leak across a `}`.
AUTO_LOGIN_CONFIG = re.compile(r"""['"]autoLogin['"]\s*:\s*\{[^{}]*$""")


def _collect_violations(content: str, pattern: re.Pattern, rel: Path, violations: list[str]) -> None:
    for match in pattern.finditer(content):
        if AUTO_LOGIN_CONFIG.search(content[max(0, match.start() - 200) : match.start()]):
            continue
        line_no = content.count("\n", 0, match.start()) + 1
        line = content.splitlines()[line_no - 1].strip()[:120]
        violations.append(f"  {rel}:{line_no}  {line}")


def test_login_payload_uses_email_not_username():
    violations = []
    for path in TESTS.rglob("*.py"):
        rel = path.relative_to(TESTS.parent)
        if any(p in str(rel).lower() for p in EXCLUDE_PATTERNS):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        _collect_violations(content, LOGIN_PAYLOAD, rel, violations)
        _collect_violations(content, LOGIN_PAYLOAD_PASSWORD_FIRST, rel, violations)
    assert not violations, (
        f"Found {len(violations)} login payloads using 'username' field.\n"
        "FastAPI login endpoint expects 'email' — change to 'email'.\n" + "\n".join(violations)
    )
