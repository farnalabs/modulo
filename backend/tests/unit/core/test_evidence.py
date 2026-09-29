"""Evidence entity tests (FAR-966 chunk 7, spec criteria E1 + E2).

Pure-unit portion of the Evidence-entity criteria:

- **E1** — append-only invariant: the ORM model exposes no update/delete
  mutation helpers, and no production code path builds an UPDATE/DELETE
  statement with the ``Evidence`` model (the append-only contract lives at
  the application layer; the DB is not the enforcement point for it).
- **E2** — RunEvidence fold-in value mapping: ``has_work`` → ``node_has_work``
  / ``True``, ``verified_empty`` → ``node_has_work`` / ``False``,
  ``unverifiable`` → ``node_has_work`` / ``None`` (JSONB null), subject shape
  ``{type: 'node_execution', id: '<run_id>:<node_id>'}``, unknown state raises.

Also asserts schema parity between the model and migration 0263: revision
linkage, CHECK-vocabulary agreement, and the table name.
"""

import importlib.util
import re
import uuid
from pathlib import Path

import pytest

from modulo.core.eval_engine.evidence_layer import EvidenceFact, map_run_evidence_to_evidence
from modulo.db.models.evidence import PRODUCER_TYPES, Evidence

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _BACKEND_ROOT / "src" / "modulo"
_MIGRATION_FILE = _BACKEND_ROOT / "src" / "modulo" / "db" / "migrations" / "versions" / "0263_evidence_layer.py"
# The single sanctioned deletion path for evidence rows (FAR-961 §2.4). Kept in
# lockstep with tests/architecture/test_evidence_deletion_carveout.py so the
# append-only E1 gate and the deletion carve-out gate cannot drift.
_EVIDENCE_SANCTIONED_DELETION = _SRC_ROOT / "core" / "evidence_retention.py"
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_NODE_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _squeeze(sql: str) -> str:
    """Collapse all whitespace so model and migration SQL compare by content."""
    return re.sub(r"\s+", "", sql)


# ---------------------------------------------------------------------------
# E1 — evidence append-only invariant (ORM/application arms)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("forbidden_name", ["update", "update_db", "delete", "delete_db"])
def test_e1_model_exposes_no_mutation_helper(forbidden_name: str) -> None:
    """The Evidence ORM class exposes no update/delete mutation helpers."""
    assert not hasattr(Evidence, forbidden_name)


def test_e1_no_production_call_site_builds_update_or_delete_against_evidence() -> None:
    """No module under ``src/modulo`` builds an UPDATE or DELETE statement
    with the ``Evidence`` model — with one reviewed carve-out.

    DELETE is sanctioned only in the retention sweep
    (``core/evidence_retention.py``, FAR-961 §2.4); UPDATE is never sanctioned
    (the append-only contract has no update writer). The carve-out is owned by
    ``tests/architecture/test_evidence_deletion_carveout.py`` — this gate must
    agree with it, or the two append-only gates drift.
    """
    update_pattern = re.compile(r"\bupdate\s*\(\s*Evidence\b")
    delete_pattern = re.compile(r"\bdelete\s*\(\s*Evidence\b")
    sanctioned = _EVIDENCE_SANCTIONED_DELETION.resolve()

    # The gate is only meaningful while the sanctioned module still owns the
    # deletion path — a silent relocation must fail loudly here.
    assert sanctioned.is_file()
    assert delete_pattern.search(sanctioned.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for py_file in _SRC_ROOT.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        # UPDATE is never sanctioned; DELETE only in the sanctioned module.
        forbidden_update = update_pattern.search(source) is not None
        forbidden_delete = py_file.resolve() != sanctioned and delete_pattern.search(source) is not None
        if forbidden_update or forbidden_delete:
            offenders.append(str(py_file))
    assert not offenders


# ---------------------------------------------------------------------------
# E2 — RunEvidence fold-in value mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("evidence_state", "expected_value"),
    [
        ("has_work", True),
        ("verified_empty", False),
        ("unverifiable", None),
    ],
)
def test_e2_fold_in_maps_state_to_value(evidence_state: str, expected_value: object) -> None:
    """Each RunEvidence evidence_state maps to key/value/subject/producer per §3.2."""
    fact = map_run_evidence_to_evidence(_RUN_ID, _NODE_ID, evidence_state)
    assert isinstance(fact, EvidenceFact)
    assert fact.key == "node_has_work"
    assert fact.value == expected_value
    assert fact.subject_type == "node_execution"
    assert fact.subject_id == f"{_RUN_ID}:{_NODE_ID}"
    assert fact.producer_type == "run"


def test_e2_fold_in_subject_id_joins_canonical_uuids_with_colon() -> None:
    """The subject_id is the run id and node id joined by a literal colon."""
    fact = map_run_evidence_to_evidence(_RUN_ID, _NODE_ID, "has_work")
    assert fact.subject_id == "00000000-0000-0000-0000-000000000001:00000000-0000-0000-0000-000000000002"


def test_e2_fold_in_accepts_string_node_id_unchanged() -> None:
    """A string node_id is passed through verbatim (no UUID re-parse)."""
    fact = map_run_evidence_to_evidence(_RUN_ID, "abc-node", "has_work")
    assert fact.subject_id == f"{_RUN_ID}:abc-node"


def test_e2_fold_in_unknown_state_raises_value_error() -> None:
    """An unknown evidence_state raises ValueError (mapped to no Evidence row)."""
    with pytest.raises(ValueError, match="Unknown evidence_state"):
        map_run_evidence_to_evidence(_RUN_ID, _NODE_ID, "impossible_state")


# ---------------------------------------------------------------------------
# Schema parity — model ↔ migration 0263
# ---------------------------------------------------------------------------


def _load_migration_module() -> object:
    """Load migration 0263 by file path so revision/linkage is asserted, not inferred."""
    spec = importlib.util.spec_from_file_location("_mig_spec_0263_evidence_layer", str(_MIGRATION_FILE))
    spec_module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(spec_module)  # type: ignore[union-attr]
    return spec_module


def test_migration_revision_chain_links_to_decision_record_payload() -> None:
    """Migration 0263 revises the HITL-gate-vocabulary head and carries its
    own revision id (chain integrity, not a dangling branch)."""
    migration = _load_migration_module()
    assert migration.revision == "0263_evidence_layer"  # type: ignore[attr-defined]
    assert migration.down_revision == "0262_hitl_gate_to_review_vocabulary"  # type: ignore[attr-defined]


def test_migration_producer_vocabulary_matches_model() -> None:
    """Migration 0263's producer-type tuple equals the model's PRODUCER_TYPES;
    the same five values drive both the CHECK constraint and the app map."""
    migration = _load_migration_module()
    migration_values = frozenset(migration._PRODUCER_TYPE_VALUES)  # type: ignore[attr-defined]
    assert migration_values == PRODUCER_TYPES


def test_migration_table_name_matches_model() -> None:
    """Migration 0263 creates exactly the table the ``Evidence`` model maps."""
    migration = _load_migration_module()
    assert Evidence.__tablename__ == migration._TABLE


def test_check_constraint_sql_matches_between_model_and_migration() -> None:
    """The model's CHECK-constraint SQL and the migration's CHECK-constraint
    SQL agree after whitespace normalisation (comma/spacing drift caught)."""
    model_check = None
    for constraint in Evidence.__table__.constraints:
        if getattr(constraint, "name", None) == "ck_evidence_producer_type":
            model_check = constraint
    assert model_check is not None
    migration = _load_migration_module()
    model_sql = _squeeze(str(model_check.sqltext))
    migration_sql = _squeeze(f"producer_type IN ({migration._producer_type_sql})")  # type: ignore[attr-defined]
    assert model_sql == migration_sql
