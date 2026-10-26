"""Structural guard: no blocking sync SDK path inside the typed-judge machinery.

ADR 066 D10 (implementation appendix): "A lint or architecture guard rejects
direct sync SDK calls inside ``typed_judge/**``, because only the async-client
rule actually prevents the wedge." A blocking call inside the judge path can
occupy a loop thread forever; the typed transport therefore only ever awaits.

This guard walks the code with ``ast`` (not a text grep), so docstrings that
*name* a forbidden pattern are ignored while real calls are caught:

* the whole ``typed_judge/`` package tree, recursively (subpackages included);
* ``pipeline_engine/llm_judge.py`` and ``eval_engine/__init__.py`` — the judge
  entry points where the sync bridge and the engine's compute path live.

It rejects blocking/synchronous network clients by IMPORT (so aliased imports
cannot slip through), plus ``asyncio.run`` / ``time.sleep`` by resolved call.
``system-one-adapter`` / the Jev SDK stay out of the runtime path (D9).
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.unit._source_scan import iter_source_text

_BACKEND = Path(__file__).resolve().parents[2]
_SOURCE = _BACKEND / "src" / "modulo"
_TYPED_JUDGE_DIR = _SOURCE / "core" / "eval_engine" / "typed_judge"
_JUDGE_MODULES = (
    _SOURCE / "core" / "pipeline_engine" / "llm_judge.py",
    _SOURCE / "core" / "eval_engine" / "__init__.py",
)

#: Module roots that would put a blocking/sync transport (or a third-party judge
#: SDK) on the judge path. ``httpx`` is included: the adapters own their async
#: clients, the judge path must never create one (sync OR async) itself.
_FORBIDDEN_MODULES = frozenset(
    {
        "requests",
        "urllib",
        "urllib3",
        "socket",
        "aiohttp",
        "subprocess",
        "httpx",
        "typesafe_sdk",
        "system_one_adapter",
    }
)
#: Call prefixes that block the running loop / create a fresh event loop.
_FORBIDDEN_CALLS = ("asyncio.run", "time.sleep")


def _target_files() -> list[Path]:
    """Every file this guard protects (typed_judge tree + judge entry modules)."""
    return sorted([*_TYPED_JUDGE_DIR.rglob("*.py"), *_JUDGE_MODULES])


def _module_root(name: str) -> str:
    return name.split(".", maxsplit=1)[0]


def _is_forbidden_module(name: str) -> bool:
    return _module_root(name) in _FORBIDDEN_MODULES


def _module_aliases(tree: ast.AST) -> tuple[dict[str, str], list[str]]:
    """Collect ``bound-name -> dotted module`` and any forbidden import roots."""
    aliases: dict[str, str] = {}
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(_import_violations(node))
            aliases.update(_import_aliases(node))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _is_forbidden_module(node.module):
                violations.append(f"imports forbidden module {node.module!r}")
            aliases.update(_from_import_aliases(node))
    return aliases, violations


def _import_violations(node: ast.Import) -> list[str]:
    return [f"imports forbidden module {alias.name!r}" for alias in node.names if _is_forbidden_module(alias.name)]


def _import_aliases(node: ast.Import) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for alias in node.names:
        bound = alias.asname or _module_root(alias.name)
        aliases[bound] = alias.name
    return aliases


def _from_import_aliases(node: ast.ImportFrom) -> dict[str, str]:
    module = node.module or ""
    return {(alias.asname or alias.name): f"{module}.{alias.name}" for alias in node.names}


def _dotted_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """Resolve a Name/Attribute chain through import aliases (``aio.run`` etc.)."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value, aliases)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _call_violations(tree: ast.AST, aliases: dict[str, str]) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted_name(node.func, aliases)
        if name is None:
            continue
        if name in _FORBIDDEN_CALLS or any(name.startswith(f"{prefix}.") for prefix in ("requests", "httpx")):
            violations.append(f"calls {name}()")
    return violations


def _violations_for_source(source: str) -> list[str]:
    """AST-level violations in one source string (also used by the self-tests)."""
    tree = ast.parse(source)
    aliases, violations = _module_aliases(tree)
    return [*violations, *_call_violations(tree, aliases)]


def _file_violations(path: Path, source: str) -> list[str]:
    return [f"{path.relative_to(_BACKEND)}: {violation}" for violation in _violations_for_source(source)]


def test_the_guard_covers_the_judge_entry_points_and_subpackages() -> None:
    targets = _target_files()
    names = {path.name for path in targets}

    assert {"bridge.py", "config.py", "transport.py"} <= names
    assert _SOURCE / "core" / "pipeline_engine" / "llm_judge.py" in targets
    assert _SOURCE / "core" / "eval_engine" / "__init__.py" in targets
    assert all(path.is_file() for path in targets)


def test_no_blocking_call_or_sync_sdk_is_used_on_the_judge_path() -> None:
    sources = list(iter_source_text(_TYPED_JUDGE_DIR)) + [
        (path, path.read_text(encoding="utf-8")) for path in _JUDGE_MODULES
    ]
    offenders = [finding for path, source in sources for finding in _file_violations(path, source)]

    assert not offenders, "blocking/sync SDK pattern(s): " + ", ".join(offenders)


def test_the_detector_flags_aliased_and_nested_forms() -> None:
    cases = {
        "import requests": "forbidden module",
        "import requests as r\nr.get('x')": "forbidden module",
        "from urllib import request": "forbidden module",
        "import httpx\nhttpx.AsyncClient()": "forbidden module",
        "import socket": "forbidden module",
        "import aiohttp": "forbidden module",
        "import subprocess": "forbidden module",
        "import system_one_adapter": "forbidden module",
        "import typesafe_sdk": "forbidden module",
        "import asyncio as aio\naio.run(main())": "asyncio.run()",
        "from asyncio import run as go\ngo(main())": "asyncio.run()",
        "import time\ntime.sleep(1)": "time.sleep()",
    }
    for source, expected in cases.items():
        findings = _violations_for_source(source)
        assert findings, f"detector missed: {source!r}"
        assert any(expected in finding for finding in findings), (source, findings)


def test_the_detector_allows_non_blocking_asyncio_use() -> None:
    source = "import asyncio\n\nasync def go():\n    await asyncio.wait_for(asyncio.sleep(0), 1)\n"

    assert not _violations_for_source(source)
