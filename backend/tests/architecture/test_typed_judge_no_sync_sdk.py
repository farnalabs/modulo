"""Structural guard: no blocking sync SDK calls inside ``typed_judge/**``.

ADR 066 D10 (implementation appendix): "A lint or architecture guard rejects
direct sync SDK calls inside ``typed_judge/**``, because only the async-client
rule actually prevents the wedge." A blocking call inside the judge path can
occupy a loop thread forever; the typed transport therefore only ever awaits.

Also pins D9: ``system-one-adapter`` / the Jev SDK is not a runtime dependency
of the typed transport.
"""

from __future__ import annotations

import re
from pathlib import Path

_TYPED_JUDGE_DIR = Path(__file__).resolve().parents[2] / "src" / "modulo" / "core" / "eval_engine" / "typed_judge"

#: Code-shaped patterns. Prose in a docstring may legitimately NAME a pattern
#: it forbids (``bridge.py`` explains why ``asyncio.run`` was removed), so the
#: matches require call syntax rather than a bare identifier.
_FORBIDDEN_PATTERNS = (
    re.compile(r"(?<!`)asyncio\.run\("),
    re.compile(r"(?<!`)time\.sleep\("),
    re.compile(r"\brequests\.(get|post|put|patch|delete|Session)\b"),
    re.compile(r"(?<!`)httpx\.Client\("),
    re.compile(r"\bsystem_one_adapter\b"),
    re.compile(r"\btypesafe_sdk\b"),
)


def _source_files() -> list[Path]:
    return sorted(_TYPED_JUDGE_DIR.glob("*.py"))


def test_typed_judge_contains_the_expected_modules() -> None:
    names = {path.name for path in _source_files()}

    assert {"bridge.py", "config.py", "transport.py"} <= names


def test_no_blocking_call_or_sync_sdk_is_used_in_typed_judge() -> None:
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.name}: {pattern.pattern}")

    assert not offenders, "blocking/SDK pattern(s) in typed_judge: " + ", ".join(offenders)
