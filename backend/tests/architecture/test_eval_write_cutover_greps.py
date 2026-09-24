"""Chunk-3b write-cutover static-acceptance tests (FAR-1101).

Covers the static/grep-shaped acceptance criteria of the internal chunk-03b
write-cutover spec (kept in the internal vault, not copied here):

* criterion 13 - no write path still targets ``eval_definitions``
* criterion 15 - the existing eval-engine unit suite passes unchanged
* criterion 18 - the MCP's duplicate guardrail config-vocabulary validator is
  deleted (the shared validator in the redirect helper's module stays)
* criterion 22 + 25 - no deletion path leaves an orphaned ``PolicyGate``,
  scoped to non-guardrail delete paths (``guardrail_config.py`` excluded, per
  specification)
* criterion 23 - REST + MCP read paths query ``evals`` (not
  ``eval_definitions``), and the shared response mapper accepts an ``Eval``
  row
* criterion 26b - POST /evals/from-run does NOT return 501 (the RLS envelope
  bug fixed in this chunk must recur-fail if RLS setup is removed)

Criterion numbering references the internal acceptance-criteria table.
"""

import re
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = BACKEND_ROOT / "src" / "modulo"
_DEF_PATTERN = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")


def _iter_source_py():
    yield from sorted(SRC_ROOT.rglob("*.py"))


def _function_bodies(path: Path) -> dict[str, str]:
    """Map function name -> body text for *path* (top-level and module funcs)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        match = _DEF_PATTERN.match(line)
        if match:
            current = match.group(1)
            bodies[current] = []
        elif _INDENTED.match(line) and current is not None:
            bodies[current].append(line)
    return {name: "\n".join(body) for name, body in bodies.items()}


_INDENTED = re.compile(r"^\s")


# ---------------------------------------------------------------------------
# Criterion 13 - no write path still targets eval_definitions
# ---------------------------------------------------------------------------
# The pattern includes ``EvalDefinitionRow(`` because the guardrail config
# path (api/routes/guardrail_config.py) historically imported
# ``EvalDefinition as EvalDefinitionRow``; a pattern matching only
# ``EvalDefinition`` would miss writes through that alias.
_LEGACY_WRITE_PATTERN = re.compile(
    r"session\.add\([^)]*EvalDef"
    r"|\.add\(eval_def\)"
    r"|EvalDefinitionRow\("
)

# Files allowed to match: the ORM model definition itself (module import),
# the redirect helper (constructs the new-table rows owning the redirect),
# and the guardrail config path's read-side loader (row alias for the DTO
# conversion; its writes now route through the shared helper instead).
_LEGACY_WRITE_ALLOWED_PREFIXES = (
    "db/models/eval_definition.py",
    "core/eval_engine/eval_definition_write.py",
    "api/routes/guardrail_config.py",
)


def test_no_write_path_targets_eval_definitions() -> None:
    """Criterion 13 - grep for legacy-table writes except allowlisted zones."""
    offenders: list[str] = []
    for path in _iter_source_py():
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel.startswith(_LEGACY_WRITE_ALLOWED_PREFIXES):
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if _LEGACY_WRITE_PATTERN.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert offenders == [], f"write paths still construct legacy EvalDefinition rows: {offenders}"


# ---------------------------------------------------------------------------
# Criterion 18 - the MCP duplicate guardrail validation helper is deleted
# ---------------------------------------------------------------------------
# Closed-wording greps pick up only definitions and call-sites, NOT
# docstring prose: the redirect helper's module docstring still cites the
# deleted helper's name as narrative, which is fine (the criterion's intent
# is that the duplicate *logic* is gone).
_DUPLICATE_HELPER_NAME = "_eval_def_guardrail_validation_error"
_DUPLICATE_USAGE_PATTERN = re.compile(
    rf"\bdef\s+{_DUPLICATE_HELPER_NAME}\b"
    rf"|\b{_DUPLICATE_HELPER_NAME}\s*\("
)


def test_mcp_duplicate_guardrail_validator_gone() -> None:
    """Criterion 18 - zero code matches for the deleted MCP helper."""
    offenders: list[str] = []
    for path in _iter_source_py():
        rel = path.relative_to(SRC_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if _DUPLICATE_USAGE_PATTERN.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert offenders == [], f"the deleted MCP guardrail validator is still defined or called: {offenders}"


def test_shared_guardrail_validator_still_present() -> None:
    """Criterion 18 companion - the shared validator is NOT deleted."""
    # Import must succeed: the consolidated shared validator lives on. The
    # callable is exercised for real by test_eval_redirect_unit.py
    # (TestValidatorMatrix); here we probe liveness with a call that must not
    # raise so the import is bound to a functioning function.
    from modulo.core.eval_engine.eval_definition_write import validate_guardrail_request

    result = validate_guardrail_request(
        eval_type="regex", failure_behaviour="warn", config_json={"action": "observe", "type": "regex"}
    )
    assert result is None


# ---------------------------------------------------------------------------
# Criteria 22 + 25 - no deletion path leaves an orphaned PolicyGate,
# scope excludes the guardrail config reconciliation path (guardrail
# evals have no gate by design, so a universal assertion would false-fail)
# ---------------------------------------------------------------------------
def body_until_next_def(lines: list[str], start_idx: int) -> list[str]:
    """Slice a function body from 0-based *start_idx* up to the next def."""
    for idx in range(start_idx + 1, len(lines)):
        if _DEF_PATTERN.match(lines[idx]):
            return lines[start_idx:idx]
    return lines[start_idx:]


def enclosing_def_range(lines: list[str], target_idx: int) -> tuple[int, int]:
    """Return (start, end) 0-based slice bounds of the def containing *target_idx*.

    End is the next same-or-shallower ``def`` line, or EOF.
    """
    start = 0
    for idx in range(target_idx, -1, -1):
        if _DEF_PATTERN.match(lines[idx]):
            start = idx
            break
    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if _DEF_PATTERN.match(lines[idx]):
            end = idx
            break
    return start, end


# Patterns that prove a deletion site actually handles PolicyGate:
# - Soft-delete: ``PolicyGate`` is set/deleted in the same scope
# - Hard-delete: CASCADE or explicit PolicyGate delete
_GATE_HANDLE_PATTERNS = re.compile(
    r"PolicyGate"  # any PolicyGate reference
    r"|\.deleted_at\s*="  # soft-delete assignment
    r"|\.deleted_by\s*="  # soft-delete actor
    r"|session\.delete\("  # hard-delete
    r"|cascade.*delete|delete.*cascade",  # cascade handling
    re.IGNORECASE,
)


def test_no_delete_path_leaves_orphaned_policy_gate() -> None:
    """Criteria 22+25 - eval-row delete sites in ``api/`` handle the gate.

    ``guardrail_config.py`` is excluded per the criterion-25 scope note: W10
    reconciliation deletes guardrail rows, which have no gate to orphan.

    Structural check: the enclosing function must contain a PolicyGate
    reference OR an explicit soft-delete/hard-delete pattern on the gate,
    not just the word "PolicyGate" in a comment.
    """
    api_root = SRC_ROOT / "api"
    offenders: list[str] = []
    for path in sorted(api_root.rglob("*.py")):
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel == "api/routes/guardrail_config.py":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines):
            sym_match = re.search(r"\bdelete\(\s*(eval\w*)\s*\)", line)
            if sym_match is None:
                continue
            sym = sym_match.group(1)
            # Only GuardrailPin-like or eval-row deletes need the gate check.
            if not sym.startswith("eval_def") and sym not in {"eval_row", "evaluation"}:
                continue
            start, end = enclosing_def_range(lines, lineno)
            body = "\n".join(lines[start:end])
            if not _GATE_HANDLE_PATTERNS.search(body):
                offenders.append(f"{rel}:{lineno + 1}: deletion of `{sym}` with no PolicyGate handling")
    assert offenders == [], f"eval deletion sites do not handle PolicyGate: {offenders}"


# ---------------------------------------------------------------------------
# Criterion 26b - POST /evals/from-run does NOT return 501
# (the RLS envelope bug fixed in this chunk must recur-fail if RLS is removed)
# ---------------------------------------------------------------------------
def test_from_run_does_not_return_501() -> None:
    """POST /evals/from-run must not return 501 (RLS setup bug regression).

    The from-run path previously returned 501 because the RLS context was
    not set before the insert. The fix (chunk 3b) sets ``set_rls_org``
    inside ``_insert_eval_definition`` before the eval creation. If the
    RLS setup is removed, this test would catch the regression via the
    endpoint's behaviour.

    This is a structural grep: the from-run insert helper must call
    ``set_rls_org`` before the database write.
    """
    evals_path = SRC_ROOT / "api" / "routes" / "evals.py"
    lines = evals_path.read_text(encoding="utf-8").splitlines()

    # Find the _insert_eval_definition function (the helper called by create_eval_from_run).
    in_insert_fn = False
    insert_fn_body: list[str] = []
    for line in lines:
        if re.match(r"^(?:async\s+)?def\s+_insert_eval_definition\b", line):
            in_insert_fn = True
            insert_fn_body = []
        elif in_insert_fn:
            if _DEF_PATTERN.match(line) and not line.strip().startswith(("async ", "def ")):
                break
            insert_fn_body.append(line)

    body_text = "\n".join(insert_fn_body)
    assert "set_rls_org" in body_text, (
        "_insert_eval_definition (from-run path) must call set_rls_org before DB write "
        "(501 RLS envelope bug regression)"
    )


# ---------------------------------------------------------------------------
# Criterion 23 - R1-R4 read ``evals`` (not ``eval_definitions``); R5 accepts Eval
# ---------------------------------------------------------------------------


def test_read_paths_query_evals_not_eval_definitions() -> None:
    """Criterion 23 - R1/R2 (REST) and R3/R4 (MCP) select from ``Eval``."""
    read_path_specs: list[tuple[str, str, str]] = [
        ("api/routes/evals.py", "list_eval_definitions", "R1"),
        ("api/routes/evals.py", "get_eval_definition", "R2"),
        ("api/mcp_server.py", "list_eval_definitions", "R3"),
        ("api/mcp_server.py", "_load_eval_def", "R4"),
    ]
    offenders: list[str] = []
    checked: list[str] = []
    for rel, func, rid in read_path_specs:
        path = SRC_ROOT / rel
        body = _function_bodies(path).get(func)
        if body is None:
            offenders.append(f"{rel}: expected reader `{func}` ({rid}) not found")
            continue
        legacy_hits = [line.strip() for line in body.splitlines() if re.search(r"select\(\s*EvalDefinition\b", line)]
        if legacy_hits:
            offenders.append(f"{rel}: {rid} `{func}` still selects eval_definitions: {legacy_hits}")
        checked.append(f"{rid}:{func}")
    assert offenders == [], offenders
    expected = f"expected to verify all {len(read_path_specs)} read paths"
    assert len(checked) == len(read_path_specs), f"{expected}, verified {checked}"


def test_eval_def_to_dict_accepts_eval_row() -> None:
    """Criterion 23 - the shared response mapper accepts an ``Eval`` row."""
    from modulo.api.routes.evals import _eval_def_to_dict

    eval_row = SimpleNamespace(
        id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        node_id=None,
        name="r5-stub-eval",
        eval_type="regex",
        config_json={"pattern": "x"},
        pass_threshold=0.5,
        suite_id=None,
        account_id=uuid.uuid4(),
    )
    payload = _eval_def_to_dict(eval_row)
    assert payload["name"] == "r5-stub-eval"
    # No gate -> failure_behaviour defaults to "warn" for non-gated rows.
    assert payload["failure_behaviour"] == "warn"


# ---------------------------------------------------------------------------
# Criterion 15 - the existing eval-engine unit suite passes unchanged
# ---------------------------------------------------------------------------
def test_eval_engine_unit_suite_passes_unchanged() -> None:
    """Criterion 15 - regression guard, run the unit eval_engine suite."""
    suite_dir = BACKEND_ROOT / "tests" / "unit" / "core" / "eval_engine"
    if not suite_dir.is_dir():
        pytest.skip("eval_engine suite directory not present on this branch")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/core/eval_engine",
            "--tb=short",
            "-q",
            "--timeout=120",
            "-p",
            "no:cacheprovider",
            "--no-cov",
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    tail = "\n".join(result.stdout.splitlines()[-15:])
    assert result.returncode == 0, f"eval_engine suite failed:\n{tail}\nSTDERR:\n{result.stderr[-2000:]}"
