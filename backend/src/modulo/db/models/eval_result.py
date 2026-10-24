import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import JSONB_COL, OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.eval_suite_run import SuiteRun


class EvalResult(OrgScoped):
    __tablename__ = "eval_results"
    __table_args__ = (
        # FAR-969: org-scoped composite FK. A plain FK to ``evals.id`` does not
        # enforce same-org binding: Postgres referential-integrity checks run
        # with elevated privilege and therefore bypass the ``rls_org_isolation``
        # policy on ``evals``. Referencing the ``(id, organisation_id)`` pair —
        # backed by ``uq_evals_id_organisation_id`` — makes a cross-organisation
        # ``eval_id`` unrepresentable, matching the pattern already used by
        # ``policy_gates`` and ``policy_gate_decisions``.
        ForeignKeyConstraint(
            ["eval_id", "organisation_id"],
            ["evals.id", "evals.organisation_id"],
            ondelete="CASCADE",
            name="fk_eval_results_eval_org",
        ),
        # ADR 066 D5/D6: ``outcome`` is the truth, with ``error`` a third state
        # meaning "no verdict was produced". Deliberately no server default —
        # a write that forgets the outcome must fail rather than record a fail.
        CheckConstraint(
            "outcome IN ('pass', 'fail', 'error')",
            name="ck_eval_results_outcome",
        ),
        # ADR 066 §0: confidence provenance is a closed vocabulary.
        CheckConstraint(
            "judge_confidence_kind IS NULL OR judge_confidence_kind IN "
            "('vendor_claimed_calibrated', 'model_self_reported', 'unavailable')",
            name="ck_eval_results_judge_confidence_kind",
        ),
        # FAR-2028: finding resolution vocabulary.
        CheckConstraint(
            "resolution IN ('accepted', 'dismissed', 'superseded', 'pending')",
            name="ck_eval_results_resolution",
        ),
        # FAR-2055: the compliance evidence index counts
        # ``count(DISTINCT eval_id)`` over an org
        # (compliance/evidence_index.py). The single-column eval_id index and
        # the (organisation_id, evaluated_at) index (migration 0218) cannot
        # answer it without a heap fetch per row; this composite makes the
        # aggregate index-only and org-bounded. Mirrors migration 0328.
        Index("ix_eval_results_org_eval_id", "organisation_id", "eval_id"),
    )

    # ``run_id`` is nullable since FAR-376: a SuiteRun-produced per-case outcome
    # is attributed to a ``suite_run`` (below), not to a pipeline ``Run``. The
    # legacy pipeline-window path still populates ``run_id``; the legacy
    # ``detect_regressions`` query joins ``runs`` (INNER JOIN) so it naturally
    # excludes the NULL-``run_id`` suite-run rows and never mixes them in.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # FAR-376: per-case outcome attribution to a ``SuiteRun``. Nullable — rows
    # produced by the legacy pipeline path keep it NULL.
    suite_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("suite_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # FAR-644: no FK to nodes.id — ``nodes`` is the deprecated composite-template
    # store; these node ids are JSON-graph ids (pipelines.graph_nodes_json) that
    # never materialise as ``nodes`` rows (see migration 0170's correction), so
    # an enforced FK would reject every legitimate write.
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    # FAR-969: ``eval_id`` is the local half of the composite org-scoped FK
    # declared in ``__table_args__`` (``fk_eval_results_eval_org``) — it carries
    # no standalone ``ForeignKey`` so the pair is enforced together.
    eval_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    # Eval-definition version snapshot (FAR-382): the integer ``version`` of the
    # eval definition that scored this result, captured at write time so a later
    # version bump (rubric change) never makes an old result look like a
    # regression. NULL for legacy rows written before versioning was cut over —
    # such rows should be resolved to the definition's current (latest) version.
    eval_definition_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ``passed`` is the compatibility projection of ``outcome`` (ADR 066 D5),
    # never the truth. ``error`` projects to False so a block gate fails closed.
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # EXPAND release (FAR-2040): nullable at the DB layer while the OLD image
    # is still serving during the rollout (it has no such column). "Every
    # writer sets outcome" is enforced by the ``before_insert`` guard below.
    # STAGE 2 flips this to NOT NULL (no server default) once the new image is
    # fully rolled out; see migration 0330's docstring.
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Schema-versioned verdict envelope (ADR 066 D5). Physical jsonb; the
    # shared JSONB_COL alias keeps SQLite/MariaDB parity.
    judge_result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB_COL, nullable=True)
    # Denormalised confidence provenance (envelope key ``confidence_kind``).
    judge_confidence_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Coded reason when ``outcome == 'error'`` (the judge sub-namespace of the
    # run error taxonomy). NULL on pass/fail.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(2000))
    observed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    # Finding resolution (FAR-2028): accepted / dismissed / superseded / pending.
    # Applies to both eval and guardrail findings (guardrails are eval_type='guardrail').
    resolution: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending", default="pending")
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )

    suite_run: Mapped["SuiteRun"] = relationship(back_populates="eval_results")


@event.listens_for(EvalResult, "before_insert")
def _require_outcome_on_insert(mapper: Any, connection: Any, target: EvalResult) -> None:
    """Application-layer NOT NULL while the DB column is still nullable.

    The 0330 EXPAND migration cannot set NOT NULL while the previous image is
    serving (that image has no ``outcome`` and would fail every insert). This
    guard is the contract in the meantime: a NEW-image write that forgets the
    value fails loudly instead of introducing a NULL. It covers ORM INSERTs
    (the only production write path); STAGE 2 makes the database enforce it.
    """
    if target.outcome is None:
        raise ValueError(
            "EvalResult.outcome is required (FAR-2040 expand contract): set it on every "
            "insert; stage 2 sets the database column NOT NULL."
        )
