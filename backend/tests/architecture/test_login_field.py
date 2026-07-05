"""Architecture test: login test payloads use 'email', not 'username'.

FastAPI's OAuth2PasswordRequestForm expects 'email' as the username field.
Test fixtures that send 'username' get a silent 422 and cascade failure.
This only flags payloads that appear to be login requests (username+password
combo sent via client.post or similar), not external API mocks.
"""

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent.parent / "tests"
# Match dict-like patterns containing both 'username' and 'password' (login payload)
LOGIN_PAYLOAD = re.compile(
    r"""['"]username['"]\s*:.*['"]password['"]""",
)
# Skip files mocking external APIs (connectors, bitbucket, gitlab, discord, etc.)
EXCLUDE_PATTERNS = ("connector", "bitbucket", "gitlab", "discord", "scanner", "ci_runner")


def test_login_payload_uses_email_not_username():
    violations = []
    for path in TESTS.rglob("*.py"):
        if any(p in path.stem.lower() for p in EXCLUDE_PATTERNS):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for i, line in enumerate(content.splitlines(), 1):
            if not LOGIN_PAYLOAD.search(line):
                continue
            violations.append(f"  {path.relative_to(TESTS.parent)}:{i}  {line.strip()[:120]}")
    assert not violations, (
        f"Found {len(violations)} login payloads using 'username' field.\n"
        "FastAPI login endpoint expects 'email' — change to 'email'.\n"
        + "\n".join(violations)
    )
