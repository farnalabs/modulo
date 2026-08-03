"""Architecture test: login test payloads use 'email', not 'username'.

FastAPI's OAuth2PasswordRequestForm expects 'email' as the username field.
Test fixtures that send 'username' get a silent 422 and cascade failure.
This only flags payloads that appear to be login requests (username+password
combo sent via client.post or similar), not external API mocks.
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
# Skip files mocking external APIs (connectors, bitbucket, gitlab, discord, etc.)
EXCLUDE_PATTERNS = ("connector", "bitbucket", "gitlab", "discord", "scanner", "ci_runner")


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
        for match in LOGIN_PAYLOAD.finditer(content):
            line_no = content.count("\n", 0, match.start()) + 1
            line = content.splitlines()[line_no - 1].strip()[:120]
            violations.append(f"  {rel}:{line_no}  {line}")
    assert not violations, (
        f"Found {len(violations)} login payloads using 'username' field.\n"
        "FastAPI login endpoint expects 'email' — change to 'email'.\n" + "\n".join(violations)
    )
