"""run_node_outputs — the per-node outputs/telemetry/markers store (FAR-583).

One row per ``(run_id, node_id, attempt_key)``. The legacy ``runs`` blob
columns (``outputs_json`` / ``node_telemetry_json`` / ``raw_output_markers``)
hold whole-run dicts keyed by node id / attempt key; this table normalises
them to one row per node so per-node reads stop loading the full blob.

Row kinds (the sentinel encoding):

* ``(run_id, node_id, '__final__')`` — the terminal per-node row for *node_id*.
  ``outputs_json`` / ``node_telemetry_json`` hold that node's value **as-is**;
  a side whose key is absent from the legacy dict is SQL NULL (absent), while
  an explicit JSON ``null`` value in the legacy dict is stored as the jsonb
  ``null`` value (present, decodes to ``None``). Readers distinguish the two
  with ``IS NULL`` flags — the SQL NULL / jsonb-null distinction is load
  bearing for the lossless legacy-bytes round-trip.
* ``(run_id, '__run_meta__', '__final__')`` — the run-level metadata row,
  written iff at least one legacy side was ``'{}'`` (non-NULL empty).
  ``outputs_json`` holds exactly ``{"empty_outputs": bool, "empty_telemetry":
  bool}`` (re-derived from full run state on every write). Legacy ``{}``
  (explicit empty dict) and legacy NULL (side absent) are DIFFERENT values —
  the metadata row is what preserves that distinction after the legacy
  columns are dropped (B2b).
* ``(run_id, <parsed node_id>, <original attempt_key>)`` — one row per legacy
  ``raw_output_markers`` key. Keys follow the ``node_runner`` grammar
  ``run:<run_id>:node:<node_id>:<suffix>``; the node id is derived by an
  anchored twin parser (literal ``run:<uuid>:node:`` prefix + split on the
  trailing ``:<suffix>``, safe for colon-containing node ids). Unparseable
  keys are preserved as evidence (FAR-188) as
  ``(run_id, '__unknown__', <full original key>)`` — never dropped, never
  aborted on.

Deliberate deviations from the ``RunEvidence`` precedent:

* ``node_id`` is a grammar-derived free-form TEXT value with NO foreign key to
  ``nodes`` — legacy dict keys are arbitrary strings (they may contain
  colons, unicode, or predate the nodes table), so an FK is impossible.
* ``created_at``/``updated_at`` (``TimestampMixin``) are added: marker rows
  arrive mid-run (distinct write instants) and the catch-up sweep needs
  ``updated_at`` for re-terminalization ghost protection. ``RunEvidence`` has
  a single ``evidence_written_at`` because it is written once, post-commit.
* ``raw_output_markers`` is typed loosely (``Any``): marker payloads are
  JSON-serialisable values (dicts today, strings historically).

The model keeps generic ``JSON`` columns (the repo parity rule: JSONB lives
only in migrations — migration 0176 promotes the three blob columns to JSONB
on Postgres and adds the STRICT sentinel-shape CHECKs there; the model carries
the portable subset). Sentinel constants live here (the DB layer) because
core imports db freely and the reverse is forbidden by importlinter.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, PrimaryKeyConstraint, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base, TimestampMixin

FINAL_ATTEMPT_KEY = "__final__"
META_NODE_ID = "__run_meta__"
UNKNOWN_NODE_ID = "__unknown__"


def json_bytes(value: Any) -> int:
    """Byte size of a JSON-serialisable value — ``len(json.dumps(value, default=str))``.

    The single shared estimator for run-blob accounting: the retention
    row-size estimate (``crud.run_retention._run_row_bytes``) and the
    ``run_node_outputs`` per-run blob-byte totals (``crud.run_node_outputs.
    read_node_output_blob_bytes``) both use this formula. Hoisted here (qa
    rider: it was duplicated as ``crud.run_retention._json_bytes`` and
    ``crud.run_node_outputs._json_bytes``); this model module is a leaf, so
    both consumers import it without a cycle.
    """
    if value is None:
        return 0
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return 0


class RunNodeOutput(Base, TimestampMixin):
    """Per-node outputs / telemetry / raw-output markers for one run.

    See the module docstring for the row kinds, the sentinel encoding, and the
    deviations from the ``RunEvidence`` precedent. Composite PK is
    ``(run_id, node_id, attempt_key)`` — there is no surrogate id (the
    natural key IS the identity, matching ``RunEvidence``). ``run_id`` needs
    no separate index: it is the PK prefix. ``organisation_id`` is the tenant
    anchor (FK -> organisations, CASCADE) and carries the one explicit index;
    the ``rls_org_isolation`` policy (migration 0176) scopes every command to
    it, and FORCE RLS means even the owner cannot bypass the policy.
    """

    __tablename__ = "run_node_outputs"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "node_id", "attempt_key", name="pk_run_node_outputs_run_node_attempt"),
        # '__final__' rows carry outputs/telemetry only — markers live on their
        # own attempt-keyed rows (a '__final__' row with markers would violate
        # the one-storey-per-row-kind encoding).
        CheckConstraint(
            "attempt_key <> '__final__' OR raw_output_markers IS NULL",
            name="ck_run_node_outputs_final_no_markers",
        ),
        # Portable subset of the metadata sentinel guard: the metadata row
        # always carries its flags payload. The STRICT shape CHECK (object
        # with EXACTLY the two boolean keys) lives in migration 0176, because
        # it needs jsonb_typeof (Postgres) / json_type (SQLite) — JSONB-only
        # SQL never belongs in the ORM model (repo parity rule).
        CheckConstraint(
            "node_id <> '__run_meta__' OR outputs_json IS NOT NULL",
            name="ck_run_node_outputs_meta_present",
        ),
    )

    # Tenant anchor — the RLS policy subject. Indexed; FK CASCADE with the
    # parent org (matches migration 0133_run_evidence_rls).
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    # Grammar-derived node id — deliberately NO FK (see module docstring).
    node_id: Mapped[str] = mapped_column(Text(), nullable=False)
    # Marker rows: the FULL original attempt key (evidence preserved, FAR-188).
    # '__final__' rows: the constant FINAL_ATTEMPT_KEY.
    attempt_key: Mapped[str] = mapped_column(Text(), nullable=False)
    outputs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    node_telemetry_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    raw_output_markers: Mapped[Any] = mapped_column(JSON, nullable=True)
