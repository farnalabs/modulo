import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped


class OrgApiKey(OrgScoped):
    __tablename__ = "org_api_keys"
    __table_args__ = (
        CheckConstraint("role IN ('operator', 'runner')", name="ck_org_api_keys_role"),
        CheckConstraint("scope IN ('org', 'user')", name="ck_org_api_keys_scope"),
        UniqueConstraint("lookup_prefix", name="uq_org_api_keys_lookup_prefix"),
        # 0271_org_api_keys_revocation_sweep_indexes — per-run key revocation
        # (auth/api_key.py::revoke_run_api_key UPDATE + revoke_run_api_key_sweep
        # join) filters (organisation_id, run_id, revoked_at IS NULL); the
        # single-column run_id/org indexes forced a bitmap-AND over the full
        # org key set.
        Index(
            "ix_org_api_keys_live_run_keys",
            "organisation_id",
            "run_id",
            postgresql_where=text("revoked_at IS NULL AND run_id IS NOT NULL"),
            sqlite_where=text("revoked_at IS NULL AND run_id IS NOT NULL"),
        ),
        # 0271 — housekeeping stale-key scan (core/housekeeping.py::
        # _scan_stale_api_keys) filters (organisation_id, revoked_at IS NULL,
        # last_used_at NULL-or-old) with no supporting index.
        Index(
            "ix_org_api_keys_stale_sweep",
            "organisation_id",
            "last_used_at",
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    lookup_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    hashed_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    # FAR-620: caller scope. 'org' = org-level caller (historical default);
    # 'user' = per-user key that operates as its creator's identity and may
    # reach caller-scoped (``.self``) MCP tools. IMMUTABLE post-mint — the
    # update paths never accept a scope payload.
    scope: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    # FAR-1477 / ADR 058: explicit capability grant-set (space-joined PERMISSIONS
    # keys, same shape as oauth_clients.scopes). TRI-STATE: NULL = legacy
    # role-bundle behaviour; "" = explicit deny-all; otherwise the exact set.
    # IMMUTABLE post-mint. NULL and "" must never be collapsed.
    grants: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Space-separated permission grants; NULL=legacy role bundle, ''=deny-all"
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), ForeignKey("runs.id", ondelete="SET NULL"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
