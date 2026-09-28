"""Architecture guards activated by FAR-1050 R6 (ADR 040 T2a conformance).

Three contracts, all fail-closed over ``backend/src``:

1. **e2b import bound form** — no ``import e2b`` / ``from e2b ...`` anywhere
   outside ``core/runtime_provider/e2b.py`` and the ADR 040 sanctioned hosts
   (the org-deletion / run-watchdog / evidence kill sites, which stay direct
   by ADR decision). Slice R6 deleted the node_runner direct path, so the
   import is now confined to exactly that set.
2. **A21 guard** — no ``apply_sandbox_policy`` import or call outside
   ``sandbox_policy.py`` (its host module) and ``core/runtime_provider/e2b.py``
   (the ``apply_isolation`` wrapper). ADR 040 makes this bound form
   end-state: it activates with the legacy-retirement slice, not before.
3. **Hostname ban** — ``api.e2b.app`` appears nowhere outside
   ``core/runtime_provider/e2b.py``. R6 deleted the legacy urllib log probe
   from ``node_runner`` (T6), which was the last island.

Each guard carries an anti-vacuity fixture (ADR 040: "the scanner extends
the existing allowlist machinery, fail-closed, with a per-clause ``tmp_path``
anti-vacuity fixture") proving the scanner actually reports a violation
instead of passing because it scanned nothing.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "modulo"

PROVIDER_MODULE = "core/runtime_provider/e2b.py"
SANDBOX_POLICY_MODULE = "core/pipeline_engine/sandbox_policy.py"

# ADR 040 sanctioned hosts that legitimately import the e2b SDK: the
# org-deletion / run-watchdog / evidence kill sites (S2/S3/S4) stay direct.
SANCTIONED_E2B_IMPORT_HOSTS = frozenset(
    {
        PROVIDER_MODULE,
        "db/crud/org_deletion.py",
        "core/pipeline_execution.py",
        "core/pipeline_engine/evidence.py",
    }
)

# A21: where ``apply_sandbox_policy`` may be imported / called.
SANCTIONED_APPLY_POLICY_HOSTS = frozenset({SANDBOX_POLICY_MODULE, PROVIDER_MODULE})

HOSTNAME = "api.e2b.app"


def _python_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "migrations" not in p.parts]


def _rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _e2b_import_violations(source: str, rel: str) -> list[str]:
    """Bound-form scan: ``import e2b`` / ``from e2b...`` in *source*.

    Returned as report lines so the same function backs the real scan and the
    anti-vacuity fixture.
    """
    violations: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - fail closed, never silently green
        return [f"  {rel}: unparsable source ({exc})"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "e2b" or alias.name.startswith("e2b."):
                    violations.append(f"  {rel}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and _is_absolute_e2b_module(node.module, node.level):
            # ``level == 0`` keeps a relative ``from .e2b`` (a local module)
            # out of scope: the bound form is the absolute SDK package.
            violations.append(f"  {rel}:{node.lineno}: from {node.module} import ...")
    return violations


def _is_absolute_e2b_module(module: str | None, level: int) -> bool:
    return level == 0 and bool(module) and (module == "e2b" or module.startswith("e2b."))


def test_no_e2b_imports_outside_the_provider_and_sanctioned_hosts() -> None:
    violations: list[str] = []
    for path in _python_files():
        rel = _rel(path)
        if rel in SANCTIONED_E2B_IMPORT_HOSTS:
            continue
        violations.extend(_e2b_import_violations(path.read_text(encoding="utf-8"), rel))
    assert not violations, (
        "ADR 040 T2a: the e2b SDK may only be imported by core/runtime_provider/e2b.py "
        "and the sanctioned kill/evidence hosts.\n"
        "Route the call through the RuntimeProvider ABC instead.\n" + "\n".join(violations)
    )


def test_e2b_import_scanner_reports_a_planted_violation(tmp_path: Path) -> None:
    """Anti-vacuity: the import scanner must flag a synthetic violation.

    Guards against the real scan passing because it scanned an empty set
    (wrong root, no .py files, swallowed parse error).
    """
    planted = tmp_path / "sneaky.py"
    planted.write_text("from e2b import AsyncSandbox\n", encoding="utf-8")
    found = _e2b_import_violations(planted.read_text(encoding="utf-8"), "sneaky.py")
    assert found, "the e2b import scanner must report a planted violation"
    assert "e2b" in found[0]

    clean = tmp_path / "clean.py"
    clean.write_text("from modulo.core.runtime_provider import RuntimeProvider\n", encoding="utf-8")
    assert not _e2b_import_violations(clean.read_text(encoding="utf-8"), "clean.py")


def _apply_policy_violations(source: str, rel: str) -> list[str]:
    """A21 bound form: importing or calling ``apply_sandbox_policy``."""
    violations: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - fail closed
        return [f"  {rel}: unparsable source ({exc})"]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.endswith("sandbox_policy")
            and any(alias.name == "apply_sandbox_policy" for alias in node.names)
        ):
            violations.append(f"  {rel}:{node.lineno}: import apply_sandbox_policy")
        elif isinstance(node, ast.Import):
            if any(alias.name.endswith("sandbox_policy") for alias in node.names):
                violations.append(f"  {rel}:{node.lineno}: import {node.names[0].name}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            if name == "apply_sandbox_policy":
                violations.append(f"  {rel}:{node.lineno}: call apply_sandbox_policy(...)")
    return violations


def test_a21_apply_sandbox_policy_confined_to_its_host_modules() -> None:
    """ADR 040 A21: the engine-side invocation retired with R6.

    ``node_runner`` imported ``apply_sandbox_policy`` until R6 removed it, so
    the bound form only turns green at the legacy-retirement slice.
    """
    violations: list[str] = []
    for path in _python_files():
        rel = _rel(path)
        if rel in SANCTIONED_APPLY_POLICY_HOSTS:
            continue
        violations.extend(_apply_policy_violations(path.read_text(encoding="utf-8"), rel))
    assert not violations, (
        "ADR 040 A21: apply_sandbox_policy may only be imported/called from "
        "core/pipeline_engine/sandbox_policy.py and core/runtime_provider/e2b.py.\n"
        "Route enforcement through RuntimeProvider.apply_isolation instead.\n" + "\n".join(violations)
    )


def test_a21_scanner_reports_a_planted_violation(tmp_path: Path) -> None:
    """Anti-vacuity for the A21 guard."""
    planted = tmp_path / "engine.py"
    planted.write_text(
        "from modulo.core.pipeline_engine.sandbox_policy import apply_sandbox_policy\n"
        "async def go(sandbox):\n"
        "    await apply_sandbox_policy(sandbox, read_only=True)\n",
        encoding="utf-8",
    )
    found = _apply_policy_violations(planted.read_text(encoding="utf-8"), "engine.py")
    assert found, "the A21 scanner must report a planted violation"
    assert any("import" in v for v in found)
    assert any("call" in v for v in found)


def test_api_e2b_app_hostname_confined_to_the_provider_module() -> None:
    violations: list[str] = []
    for path in _python_files():
        rel = _rel(path)
        if rel == PROVIDER_MODULE:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if HOSTNAME in line:
                violations.append(f"  {rel}:{lineno}: {HOSTNAME}")
    assert not violations, (
        "ADR 040: the api.e2b.app hostname belongs only to core/runtime_provider/e2b.py "
        "(R6 deleted the legacy node_runner log probe).\n" + "\n".join(violations)
    )


def test_hostname_scanner_reports_a_planted_violation(tmp_path: Path) -> None:
    """Anti-vacuity for the hostname ban."""
    planted = tmp_path / "probe.py"
    planted.write_text(f'URL = "https://{HOSTNAME}/sandboxes"\n', encoding="utf-8")
    found = [
        f"  probe.py:{lineno}"
        for lineno, line in enumerate(planted.read_text(encoding="utf-8").splitlines(), start=1)
        if HOSTNAME in line
    ]
    assert found, "the hostname scanner must report a planted violation"
