import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from modulo.db.models.base import OrgScoped

if TYPE_CHECKING:
    from modulo.db.models.eval_suite_run import SuiteRun


class EvalResult(OrgScoped):
    __tablename__ = "eval_results"

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
    node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("nodes.id", ondelete="SET NULL"))
    eval_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("eval_definitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Eval-definition version snapshot (FAR-382): the integer ``version`` of the
    # eval definition that scored this result, captured at write time so a later
    # version bump (rubric change) never makes an old result look like a
    # regression. NULL for legacy rows written before versioning was cut over —
    # such rows should be resolved to the definition's current (latest) version.
    eval_definition_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[str | None] = mapped_column(String(2000))
    observed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )

    suite_run: Mapped["SuiteRun"] = relationship(back_populates="eval_results")
