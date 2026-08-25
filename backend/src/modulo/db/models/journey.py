"""Journeys — the work-item journey row minted at create-time (FAR-142).

A journey is the canonical record of "one piece of work" (a GitHub issue, a
Linear ticket, ...) across every Modulo run that touches it. The row is MINTED
at create time from the run's canonicalised work-item refs via
``INSERT ... ON CONFLICT (organisation_id, kind, ref) DO NOTHING`` — mint-only,
never touching ``latest_*`` or ``run_count`` (those are owned by the finalise
path, FAR-143).

``canonical_work_item_id`` IS the deterministic canonical id
(``uuid5(org, kind, ref)``) — the same (org, kind, ref) always yields the same
id, so there is no mint race and no overwrite.

``latest_terminal_run_id`` is deliberately NOT a FK (the ``run_daily_facts``
precedent): the journey must survive the 90-day run purge, so a future "fix"
into an FK breaks retention.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class Journey(OrgScoped):
    __tablename__ = "journeys"
    __table_args__ = (UniqueConstraint("organisation_id", "kind", "ref", name="uq_journeys_org_kind_ref"),)

    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    ref: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_work_item_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    latest_terminal_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        nullable=True,
        comment=("deliberately NOT a FK — journeys survive the run purge; a future 'fix' into an FK breaks retention"),
    )
    map_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    map_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stage_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    latest_provenance: Mapped[str | None] = mapped_column(String(30), nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
