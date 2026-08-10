"""Document the intent of the ``bare-raise-in-except`` semgrep rule.

The rule lives at ``.semgrep/bare-raise-in-except.yml`` and uses
``pattern-regex``. It exists to catch a *silent re-raise*: an ``except``
handler whose body is only a bare ``raise`` with no logging and no
``... from ...`` context, which hides the original error's cause.

The rule is deliberately scoped so that re-raising a **concrete** exception
type is NOT flagged. In particular ``except HTTPException: raise`` is the
correct FastAPI idiom — the handler catches ``HTTPException`` (so earlier
broader handlers like ``except SQLAlchemyError`` / ``except Exception`` do
not swallow it) and re-raises it so FastAPI's exception handler maps the
status code. Flagging that pattern would be a false positive.

These tests exercise the rule's regex logic directly (semgrep-core cannot
run on Windows). They assert the dangerous case still matches and the
legitimate HTTPException re-raise does not.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
RULE_FILE = REPO_ROOT / ".semgrep" / "bare-raise-in-except.yml"


def _load_rule_regex() -> re.Pattern[str]:
    with RULE_FILE.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    rule = next(r for r in doc["rules"] if r["id"] == "bare-raise-in-except")
    return re.compile(rule["pattern-regex"])


RULE_REGEX = _load_rule_regex()


def _matches(source: str) -> bool:
    return RULE_REGEX.search(source) is not None


DANGEROUS_SNIPPETS = [
    (
        "bare raise inside except Exception is a silent re-raise",
        ("async def handler():\n    try:\n        await work()\n    except Exception:\n        raise\n"),
    ),
    (
        "bare raise inside except BaseException is a silent re-raise",
        ("async def handler():\n    try:\n        await work()\n    except BaseException:\n        raise\n"),
    ),
    (
        "bare raise still flagged when comment lines precede it",
        (
            "async def handler():\n"
            "    try:\n"
            "        await work()\n"
            "    except Exception:\n"
            "        # nothing to log here\n"
            "        raise\n"
        ),
    ),
    (
        "bare raise with bound name is still a silent re-raise",
        ("async def handler():\n    try:\n        await work()\n    except Exception as exc:\n        raise\n"),
    ),
]

LEGITIMATE_SNIPPETS = [
    (
        "except HTTPException bare raise is the FastAPI re-raise idiom",
        ("async def handler():\n    try:\n        await work()\n    except HTTPException:\n        raise\n"),
    ),
    (
        "except HTTPException re-raise after broader handlers is valid",
        (
            "async def handler():\n"
            "    try:\n"
            "        await work()\n"
            "    except SQLAlchemyError:\n"
            "        logger.exception('db down')\n"
            "        raise HTTPException(status_code=503) from None\n"
            "    except HTTPException:\n"
            "        raise\n"
        ),
    ),
    (
        "logging before re-raise is not a silent re-raise",
        (
            "async def handler():\n"
            "    try:\n"
            "        await work()\n"
            "    except Exception:\n"
            "        logger.exception('work failed')\n"
            "        raise\n"
        ),
    ),
    (
        "raise from None supplies explicit context",
        (
            "async def handler():\n"
            "    try:\n"
            "        await work()\n"
            "    except Exception as exc:\n"
            "        raise HTTPException(status_code=500) from None\n"
        ),
    ),
    (
        "re-raise of a concrete ValueError is a targeted re-raise",
        ("async def handler():\n    try:\n        await work()\n    except ValueError:\n        raise\n"),
    ),
]


@pytest.mark.parametrize(("name", "source"), DANGEROUS_SNIPPETS)
def test_dangerous_silent_reraise_is_flagged(name: str, source: str) -> None:
    assert _matches(source), f"rule should flag: {name}"


@pytest.mark.parametrize(("name", "source"), LEGITIMATE_SNIPPETS)
def test_legitimate_reraise_is_not_flagged(name: str, source: str) -> None:
    assert not _matches(source), f"rule should NOT flag: {name}"
