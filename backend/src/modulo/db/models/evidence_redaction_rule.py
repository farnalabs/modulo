"""Per-org, per-role, per-key-pattern evidence redaction rules (FAR-944, chunk 9b §2.4).

The redaction boundary (§2.2) is a server-side rule that decides which fields of
an evidence row are visible to which role. This table is the configuration that
feeds it: for a given ``(role, key_pattern)`` it declares the redaction CLASSES
(``sensitive_output`` / ``internal_metadata`` — see
:mod:`modulo.core.evidence_redaction`) whose fields are replaced with the
``"[REDACTED]"`` sentinel before the payload is serialised.

This is a small configuration table (tens of rows), not an append-only fact
store: org admins edit it through ``PUT /api/v1/evidence/redaction-rules``. It
follows the same soft-delete + OrgScoped convention as the other configuration
tables (e.g. ``parameter_schemas``, ``environment_profiles``) — CO-3.

RLS org-isolation is enforced by migration ``0320_evidence_redaction_rules``.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import CheckConstraint, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped, SoftDeleteMixin

#: Authoritative role vocabulary — must match the CHECK constraint (§2.4).
#: A fixed set (foreclosure ledger): adding a role requires a migration that
#: widens the CHECK.
REDACTION_RULE_ROLES: Final[frozenset[str]] = frozenset({"admin", "operator", "viewer"})

#: The match-everything key pattern of the mandatory §2.4 catch-all seed rule.
#: Shared with the redaction service and the org-creation seed so the pattern
#: cannot drift between the migration, the seeder and the resolver.
EVIDENCE_REDACTION_CATCH_ALL_PATTERN: Final[str] = "*"

#: The role the mandatory catch-all seed rule targets (§2.4).
EVIDENCE_REDACTION_CATCH_ALL_ROLE: Final[str] = "viewer"

#: The redaction classes the mandatory catch-all seed rule applies (§2.4).
CATCH_ALL_REDACTED_FIELDS: Final[tuple[str, ...]] = ("sensitive_output", "internal_metadata")


class EvidenceRedactionRule(SoftDeleteMixin, OrgScoped):
    """A redaction declaration for one ``(organisation, key_pattern, role)``.

    ``redacted_fields`` stores redaction CLASS names (not raw JSON keys) — the
    class -> field mapping lives in :data:`modulo.core.evidence_redaction.REDACTION_CLASS_FIELDS`.
    """

    __tablename__ = "evidence_redaction_rules"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'operator', 'viewer')",
            name="ck_evidence_redaction_rules_role",
        ),
        UniqueConstraint(
            "organisation_id",
            "key_pattern",
            "role",
            name="uq_evidence_redaction_rules_org_key_role",
        ),
    )

    key_pattern: Mapped[str] = mapped_column(Text(), nullable=False)
    role: Mapped[str] = mapped_column(Text(), nullable=False)
    redacted_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text()),
        nullable=False,
        default=list,
        server_default=text("'{}'"),
    )
