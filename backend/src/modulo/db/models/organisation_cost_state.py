"""Per-organisation cost state — spend ceilings + cumulative accrual (FAR-1833).

One row per organisation, keyed directly by ``org_id`` (no surrogate id). A
MISSING ROW means the defaults: both ceilings unlimited (NULL) and cumulative
spend 0. Accrual and ceiling writes UPSERT, so the row appears on first write.

This table is the cutover of FAR-1823 Land 2: the finalize gate+accrual and
the admin ceiling set/clear endpoints read and write it under a per-org
advisory lock (``modulo.core.runner_capacity.lock_org_cost_state``), so the hot
path never updates — and never takes a row lock on — the ``organisations`` row
(the FAR-1823 / D9 lock-coupling shape). Migration ``0321`` creates and
backfills it. The three same-named ``organisations`` columns are deliberately
LEFT IN PLACE by that migration (the expand half of an expand/contract cutover);
a follow-up migration drops them once this release is live, so no rolling-deploy
window is created.

The table is deliberately keyed ``org_id`` and NOT ``organisation_id``, so it
sits outside the generic ``organisation_id`` RLS sweep by construction. It is
STILL row-level-secured: migration ``0321`` ENABLEs (and FORCEs) RLS with an
explicit ``org_id`` policy, because on Postgres the ORM tenant filter is a no-op
(``db/rls.py`` returns early for ``postgres``), so RLS is the only DB-level
backstop. Every production path here (the admin cost routes, the finalize
gate+accrual, the executor pre-dispatch gate) runs under ``set_rls_org``.

Earlier revisions of this docstring drew a parallel to ``system_audit_events``;
that analogy was INAPT. ``system_audit_events`` is append-only, has no FK and
must OUTLIVE org deletion, so it deliberately sits outside RLS. This table is the
opposite — mutable, ``ON DELETE CASCADE``, and it MUST NOT survive its org — so
it is a normal org-scoped table and RLS is correct.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import Base


class OrganisationCostState(Base):
    __tablename__ = "organisation_cost_state"
    __table_args__ = (
        CheckConstraint(
            "org_cumulative_spend_cents >= 0",
            name="ck_organisation_cost_state_cum_spend",
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organisations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Per-run hard ceiling; None = unlimited.
    max_run_cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Org lifetime budget; None = unlimited. A ceiling of 0 is a deliberate
    # kill-switch (blocks every billable run).
    spend_ceiling_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Running consumed total, incremented atomically at each run's terminal
    # ledger write. NOT NULL with a server default so a raw INSERT that omits
    # it starts from 0 rather than violating the column.
    org_cumulative_spend_cents: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
