"""``external_agents`` — inventory of NON-Modulo agents (FAR-1692 S1).

An external agent is any agent that Modulo neither dispatched nor executed
(Claude Code, Cursor, a local opencode session, ...). This table is the
*inventory* half of FAR-1692: a record of what exists, not an execution. It is
**policy substrate** (ADR 055: record how we know, do not police the agent) —
rows are informational and never themselves enforce anything.

Deliberately distinct from:

* ``agents`` — Modulo's OWN agent-definition table (a node's prompt/model),
  not an external runtime;
* ``agent_runner_bindings`` — a credential binding for a Modulo-owned runner;
* ``node_observations`` — a run-scoped human observation.

Scoping: org-scoped (``OrgScoped``) with a team-visibility pair
(``visibility``/``owner_team_id``). The table is enforced by DB RLS alone
(``rls_team_isolation``, created in migration ``0313``), the same
DB-RLS-only posture the core team-scoped tables take (FAR-1514).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin


class ExternalAgent(SoftDeleteMixin, OrgScoped):
    __tablename__ = "external_agents"
    __table_args__ = (
        CheckConstraint("visibility IN ('org', 'team')", name="ck_external_agents_visibility"),
        CheckConstraint(
            "visibility = 'org' OR owner_team_id IS NOT NULL",
            name="ck_external_agents_team_owner",
        ),
        CheckConstraint(
            "runtime_kind IN ('claude-code', 'cursor', 'opencode', 'other')",
            name="ck_external_agents_runtime_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'unknown')",
            name="ck_external_agents_status",
        ),
        CheckConstraint(
            "discovery_provenance IN ('manual', 'mcp', 'webhook', 'forge')",
            name="ck_external_agents_discovery_provenance",
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # The agent runtime family; ``other`` keeps an unknown/novel client
    # recordable rather than forcing it into a wrong bucket.
    runtime_kind: Mapped[str] = mapped_column(String(50), nullable=False, server_default="other")
    # Free-form owner/host descriptor (workstation name, container, hostname).
    host_descriptor: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="active")
    # How this row became known: manual entry today; ``mcp``/``webhook``/``forge``
    # are the ingestion legs of later slices (S3/S4) and populate ``first_seen``.
    discovery_provenance: Mapped[str] = mapped_column(String(30), nullable=False, server_default="manual")
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="SET NULL"), index=True
    )
