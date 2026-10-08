"""FAR-1565: ONE run-provenance serializer, enforced structurally.

Two layers of guard:

1. **The serializer's own contract** (behavioural): ``run_provenance_fields``
   returns exactly ``{"execution_origin": str | None}`` for a dispatched run, an
   executed run, a row that never carried the column, and a missing row — with
   NO ``isinstance(str)`` coercion (FAR-1566 removed the test-double tolerance
   that used to live in every surface's ad-hoc copy).

2. **The surface set** (structural, replacing the hand-enumerated list of
   surfaces): an AST scan of ``src/modulo/api/**`` and
   ``src/modulo/core/pipeline_engine/**`` asserts, mechanically rather than by
   prose or by a maintained list, that

   * no surface hand-writes the ``execution_origin`` key/keyword — every value
     must derive from ``run_provenance_fields`` (the 12 ad-hoc copies
     FAR-1565 collapsed cannot come back); and
   * every run-shaped payload builder (a dict/constructor call carrying run
     identity + a run-status + a run-marker key) composes the serializer — so a
     13th run-rendering surface cannot forget the field.

Both structural checks fail against the pre-FAR-1565 tree (hand-rolled keys and
run-shaped payloads with no serializer call), so a revert of the refactor is
caught here, not by review.

The scope deliberately excludes ``core/analytics`` (its ``execution_origin``
dict is a ``run_daily_facts`` upsert — a DB write, not a run payload) and the
per-surface behaviour itself, which
``tests/unit/api/test_execution_origin_surfaces.py`` and the endpoint suites
pin with real rows.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from modulo.core.run_provenance import run_provenance_fields

_BACKEND = Path(__file__).resolve().parents[3]
_SRC = _BACKEND / "src" / "modulo"

# The run-rendering layers: every REST/MCP/dashboard/slack/lifecycle surface
# lives under api/, the run-keyed audit payloads under core/pipeline_engine/.
_SCOPE_DIRS = (_SRC / "api", _SRC / "core" / "pipeline_engine")

_SERIALIZER = "run_provenance_fields"

# Keys that mark a payload as "rendering a run": a run identity + a status.
_IDENTITY_KEYS = frozenset({"run_id", "id"})
_STATUS_KEY = "status"
# Markers that distinguish a full run render from a bare ack/status snippet
# (webhook acks, HITL claim items and trigger-event rows also carry
# run_id+status but are NOT run renders and must not be forced to compose).
_RUN_RENDER_MARKERS = frozenset({"trigger_type", "provenance"})


def _scope_files() -> list[Path]:
    files: list[Path] = []
    for scope in _SCOPE_DIRS:
        files.extend(sorted(scope.rglob("*.py")))
    return files


def _rel(path: Path) -> str:
    return str(path.relative_to(_BACKEND)).replace("\\", "/")


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _composes_serializer(node: ast.AST) -> bool:
    """True when the node contains a ``run_provenance_fields(...)`` call."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _dotted(child.func)
            if name is not None and name.split(".")[-1] == _SERIALIZER:
                return True
    return False


def _literal_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _keyword_names(node: ast.Call) -> set[str]:
    return {k.arg for k in node.keywords if k.arg is not None}


# ---------------------------------------------------------------------------
# Structural guard 1 — nobody may hand-roll the provenance key
# ---------------------------------------------------------------------------


def test_no_surface_hand_writes_the_execution_origin_key() -> None:
    """Every supply of ``execution_origin`` in the run-rendering layers must
    derive from ``run_provenance_fields``.

    This is the collapse FAR-1565 exists for: the field used to be written by
    hand (with a per-surface ad-hoc coercion) in a dozen payload builders. A
    new hand-rolled copy — or a regression to one — fails here by file:line.
    """
    offenders: list[str] = []
    for path in _scope_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                if "execution_origin" in _literal_keys(node) and not _composes_serializer(node):
                    offenders.append(f"{_rel(path)}:{node.lineno} dict literal key")
            elif isinstance(node, ast.Call):
                kwargs = _keyword_names(node)
                hand_rolled_kwarg = "execution_origin" in kwargs and not _composes_serializer(node)
                if hand_rolled_kwarg:
                    offenders.append(f"{_rel(path)}:{node.lineno} kwarg of {_dotted(node.func)}()")
    assert not offenders, (
        "execution_origin must be supplied ONLY via run_provenance_fields() "
        "(FAR-1565); these sites hand-roll it:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Structural guard 2 — run-shaped payloads must compose the serializer
# ---------------------------------------------------------------------------


def test_run_shaped_payloads_compose_the_shared_serializer() -> None:
    """A run-rendering payload builder that does not compose the serializer
    would silently drop provenance — the failure mode a prose rule ("remember
    to add execution_origin") cannot catch.

    Detection is by SHAPE, not by a maintained file list: a dict/constructor
    call that carries run identity (``run_id``/``id``) + ``status`` + a
    run-render marker (``trigger_type``/``provenance``) is a run render. Bare
    acks and status snippets (``{run_id, status}``) are deliberately out of
    scope — they are not claim-ready run surfaces.
    """
    missing: list[str] = []
    for path in _scope_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = _literal_keys(node)
                if (
                    _STATUS_KEY in keys
                    and keys & _IDENTITY_KEYS
                    and keys & _RUN_RENDER_MARKERS
                    and not _composes_serializer(node)
                ):
                    missing.append(f"{_rel(path)}:{node.lineno} dict literal {sorted(keys)}")
            elif isinstance(node, ast.Call):
                kwargs = _keyword_names(node)
                if (
                    _STATUS_KEY in kwargs
                    and "run_id" in kwargs
                    and kwargs & _RUN_RENDER_MARKERS
                    and not _composes_serializer(node)
                ):
                    missing.append(f"{_rel(path)}:{node.lineno} {_dotted(node.func)}()")
    assert not missing, (
        "this run-rendering payload does not compose run_provenance_fields() "
        "and would ship without execution_origin:\n  " + "\n  ".join(missing)
    )


def test_the_structural_scan_sees_the_expected_surface_layers() -> None:
    """Guard the guard: the AST scan must actually be looking at the layers it
    claims to (a silently-empty scope would make both checks vacuous)."""
    scanned = [_rel(p) for p in _scope_files()]
    assert scanned, "structural scan found no source files"
    assert any(p.startswith("src/modulo/api/routes/") for p in scanned)
    assert any(p == "src/modulo/api/mcp_server.py" for p in scanned)
    assert any(p.startswith("src/modulo/core/pipeline_engine/") for p in scanned)


# ---------------------------------------------------------------------------
# The serializer's contract
# ---------------------------------------------------------------------------


def _run(**overrides: Any) -> SimpleNamespace:
    row = SimpleNamespace(
        id=uuid4(),
        pipeline_id=uuid4(),
        status="complete",
        trigger_type="manual",
        execution_origin=None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class TestRunProvenanceFields:
    def test_wire_shape_is_exactly_execution_origin(self) -> None:
        assert set(run_provenance_fields(_run(execution_origin="dispatched"))) == {"execution_origin"}

    def test_a_dispatched_run_reads_dispatched(self) -> None:
        assert run_provenance_fields(_run(execution_origin="dispatched")) == {"execution_origin": "dispatched"}

    def test_an_executed_run_reads_null(self) -> None:
        assert run_provenance_fields(_run(execution_origin=None)) == {"execution_origin": None}

    def test_a_row_that_never_carried_the_column_reads_null(self) -> None:
        """Pre-migration / partially-projected rows must read NULL, never raise
        — provenance must never break a read."""
        row = _run()
        del row.execution_origin
        assert run_provenance_fields(row) == {"execution_origin": None}

    def test_a_missing_row_reads_null_so_the_key_is_never_omitted(self) -> None:
        """The eval.blocked audit's degrade arm passes ``None`` after a failed
        origin read: the key stays present with NULL."""
        assert run_provenance_fields(None) == {"execution_origin": None}

    def test_there_is_no_test_double_coercion(self) -> None:
        """FAR-1566: a non-string value passes through UNCHANGED instead of
        being silently rewritten to ``None`` — the response models own the
        ``str | None`` wire contract and reject a violation loudly."""
        assert run_provenance_fields(_run(execution_origin=12345)) == {"execution_origin": 12345}
