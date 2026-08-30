import uuid
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class PipelineEdge(OrgScoped):
    __tablename__ = "pipeline_edges"
    __table_args__ = (
        CheckConstraint(
            "edge_type IN ('normal', 'reject', 'conditional')",
            name="ck_pipeline_edges_type",
        ),
        UniqueConstraint(
            "pipeline_id",
            "source_node_id",
            "target_node_id",
            "edge_type",
            name="uq_pipeline_edges_path",
        ),
    )

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("pipelines.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    target_node_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)
    edge_type: Mapped[str] = mapped_column(String(15), nullable=False, server_default="normal")
    # FAR-416 (FAR-402 F1): port addressing over the flat run_context/artifact
    # dict. Defaults mirror the pre-port flat-state keys so legacy edges route
    # identically. Migration 0141 adds these NOT NULL columns but intentionally
    # DROPs the DB-level default so the constraint is enforced on new rows; a
    # server_default here would make the ORM omit the column from INSERTs (and
    # then hit the NOT NULL violation on rows that don't set a port explicitly,
    # e.g. clone). Use a Python-side default so the value is always emitted on
    # INSERT while still allowing explicit port values to override it.
    source_port: Mapped[str] = mapped_column(String(64), nullable=False, default="out")
    target_port: Mapped[str] = mapped_column(String(64), nullable=False, default="in")
    # MutableDict.as_mutable(JSON): in-place gate-config mutations are tracked
    # as dirty (hitl-gate-removal-guard-plan.md v19 §3 item 7 — defense in
    # depth so a load-then-mutate pattern can never silently bypass a write).
    hitl_gate_config: Mapped[dict[str, Any] | None] = mapped_column(MutableDict.as_mutable(JSON))
    condition_expression: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # FAR-402 P5 (§4F): a transition-edge retry block (re-executes the source
    # node) and an ``on_failure_target`` (a compensation node). Mutually
    # exclusive per failure — the GraphValidator emits a typed error if both are
    # set on the same edge. Generic JSON for cross-backend parity.
    retry: Mapped[dict[str, Any] | None] = mapped_column(MutableDict.as_mutable(JSON), nullable=True)
    on_failure_target: Mapped[str | None] = mapped_column(String(64), nullable=True)
