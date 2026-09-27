"""Generic Evidence layer — fetch, authorisation, and RunEvidence fold-in (FAR-966 chunk 7).

This module provides three capabilities for the generic Evidence layer:

- **Subject-scoped fetch** (§6): ``fetch()`` queries the evidence table
  for the most recent row per distinct key for a given subject, scoped by
  organisation.  Uses ``DISTINCT ON`` for cross-key snapshot consistency.
- **Producer write-authorisation** (§5): ``assert_write_authorisation()``
  enforces key-namespace ownership via the ``KEY_NAMESPACE_OWNERSHIP`` map.
  The CHECK constraint on ``producer_type`` (§3.1) is authoritative for the
  valid producer-type vocabulary; this map is application-level only.
- **RunEvidence fold-in mapping** (§3.2): ``map_run_evidence_to_evidence()``
  translates existing ``RunEvidence`` tri-state rows into the generic
  Evidence format.

**Deferred:** predicate language, ``decide`` function, compatibility adapter
(§4 — parked unit).

**Dormancy declaration:** nothing writes evidence in production until the
guardrail-mapping chunk (chunk 8), except the run-evidence fold-in if it
migrates existing writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.evidence import Evidence

# ---------------------------------------------------------------------------
# §6 — Producer write-authorisation (key-namespace ownership)
# ---------------------------------------------------------------------------

#: The authorisation map: ``producer_type`` -> set of allowed key prefixes.
# Every ``producer_type`` here MUST appear in the CHECK constraint (§3.1).
KEY_NAMESPACE_OWNERSHIP: dict[str, set[str]] = {
    "eval": {"eval_"},
    "system_state": {"connector_", "sandbox_", "capability_"},
    "run": {"node_"},
    "policy_gate": set(),  # reserved, empty in chunk 7
    "derived": set(),  # reserved, empty in chunk 7
}


class EvidenceWriteAuthorisationError(Exception):
    """Raised when a producer attempts to write an unauthorised key."""


def assert_write_authorisation(producer_type: str, key: str) -> None:
    """Raise ``EvidenceWriteAuthorisationError`` if *producer_type* is not
    permitted to write *key*.  Called by every evidence write path.

    The CHECK constraint on ``evidence.producer_type`` (§3.1) is
    authoritative for the valid producer-type vocabulary.  This
    application-level map enforces key-prefix ownership within that
    vocabulary.      **Known residual** (§5.4): a DB-level second layer is
    accepted as unnecessary for this slice because the key-prefix
    vocabulary grows across chunks.
    """
    allowed = KEY_NAMESPACE_OWNERSHIP.get(producer_type)
    if allowed is None:
        raise EvidenceWriteAuthorisationError(f"Unknown producer type: {producer_type}")
    if not any(key.startswith(prefix) for prefix in allowed):
        raise EvidenceWriteAuthorisationError(
            f"Producer '{producer_type}' is not authorised to write key '{key}'. Allowed prefixes: {allowed}"
        )


# ---------------------------------------------------------------------------
# §5/§6 — Subject-scoped fetch
# ---------------------------------------------------------------------------


async def fetch(
    subject_type: str,
    subject_id: str,
    org_id: UUID,
    db: AsyncSession,
) -> list[Evidence]:
    """Subject-scoped DB query.  Returns the most recent Evidence row per
    distinct key for the given subject.

    Uses ``DISTINCT ON (organisation_id, subject_type, subject_id, key)``
    ``ORDER BY organisation_id, subject_type, subject_id, key,
    created_at DESC, id DESC``.  Scoped by organisation via ``WHERE``
    clause AND included in ``DISTINCT ON`` so the dedup is
    tenant-scoped by construction — never touches ``predicate_ast``.

    May raise on DB errors — callers must not let a fetch failure
    bypass the policy (§6.2 fetch-failure contract).
    """
    stmt = (
        select(Evidence)
        .where(
            Evidence.organisation_id == org_id,
            Evidence.subject_type == subject_type,
            Evidence.subject_id == subject_id,
        )
        .order_by(
            Evidence.organisation_id,
            Evidence.subject_type,
            Evidence.subject_id,
            Evidence.key,
            Evidence.created_at.desc(),
            Evidence.id.desc(),
        )
        .distinct(
            Evidence.organisation_id,
            Evidence.subject_type,
            Evidence.subject_id,
            Evidence.key,
        )
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# §3.2 — RunEvidence fold-in mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceFact:
    """A single evidence fact ready for write into the Evidence table.

    Carries the mapped key, value, and subject from a RunEvidence row.
    """

    key: str
    value: bool | None  # True = has_work, False = verified_empty, None = unverifiable
    subject_type: str
    subject_id: str
    producer_type: str = "run"


# Mapping from RunEvidence.evidence_state -> Evidence value.
_RUN_EVIDENCE_VALUE_MAP: dict[str, bool | None] = {
    "has_work": True,
    "verified_empty": False,
    "unverifiable": None,
}


def map_run_evidence_to_evidence(
    run_id: UUID,
    node_id: UUID | str,
    evidence_state: str,
) -> EvidenceFact:
    """Map a single RunEvidence row to the generic Evidence format (§3.2).

    The mapping:
    - ``has_work``  -> key=``node_has_work``, value=``true``
    - ``verified_empty`` -> key=``node_has_work``, value=``false``
    - ``unverifiable`` -> key=``node_has_work``, value=``null`` (JSONB)

    Subject shape: ``{type: "node_execution", id: "{run_id}:{node_id}"}``
    with UUIDs in canonical lowercase hyphenated form joined by literal ``:``.

    Raises ``ValueError`` for unknown evidence_state values.
    """
    if evidence_state not in _RUN_EVIDENCE_VALUE_MAP:
        raise ValueError(f"Unknown evidence_state: {evidence_state!r}")

    node_id_str = str(node_id) if not isinstance(node_id, str) else node_id
    return EvidenceFact(
        key="node_has_work",
        value=_RUN_EVIDENCE_VALUE_MAP[evidence_state],
        subject_type="node_execution",
        subject_id=f"{run_id}:{node_id_str}",
    )
