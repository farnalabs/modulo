"""Plan decision engine for ``modulo apply`` (FAR-681, slice 1).

Name-based upsert scoped to the org (the HTTP transport is RLS-bound). A
decision is one of:

- ``created``  -- entity is absent in the org
- ``updated``  -- present, managed-field canonical hash differs from desired
- ``unchanged``-- present, canonical hash equal
- ``blocked``  -- incompatible shape (e.g. provider mismatch) or unresolved ref

Output shape (also the golden-snapshot shape)::

    {
      "created":   [{"kind": ..., "name": ...}],
      "updated":   [{...}],
      "unchanged": [{...}],
      "blocked":   [{"kind": ..., "name": ..., "reason": ...}],
    }
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


def canonical_hash(payload: dict[str, Any]) -> str:
    """Canonical-JSON sha256 of a managed-field payload (migrate_org pattern)."""
    serialised = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Decision:
    """A single entity's plan outcome."""

    kind: str
    name: str
    status: str
    reason: str | None = None
    desired_hash: str | None = None
    current_hash: str | None = None


KIND_SCHEMA = "schema"
KIND_BACKEND = "model_backend"


def _schema_current_view(current: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": current.get("description"),
        "abstract_name": current.get("abstract_name"),
        "versions": sorted(
            current.get("versions") or [],
            key=lambda v: (v.get("version", ""), v.get("version_number", 0)),
        ),
    }


def _backend_current_view(current: dict[str, Any]) -> dict[str, Any]:
    return {
        "display_name": current.get("display_name"),
        "provider": current.get("provider"),
        "model_id": current.get("model_id"),
        "default_params": current.get("default_params") or {},
        "visibility": current.get("visibility"),
        "tier": current.get("tier"),
    }


_CURRENT_VIEWS: dict[str, type] = {
    KIND_SCHEMA: _schema_current_view,
    KIND_BACKEND: _backend_current_view,
}


def plan_entity(
    kind: str,
    name: str,
    desired_view: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> Decision:
    """Decide the outcome for one entity.

    ``desired_view`` is the entity's managed_view(); ``current`` is the raw
    entity map fetched from the API (or None when absent).
    """
    if desired_view is None:
        msg = f"desired entity not recognised for kind {kind}"
        raise ValueError(msg)
    desired_hash = canonical_hash(desired_view)
    if current is None:
        return Decision(
            kind=kind,
            name=name,
            status="created",
            desired_hash=desired_hash,
        )
    if kind == KIND_BACKEND:
        desired_provider = desired_view.get("provider")
        current_provider = current.get("provider")
        if desired_provider != current_provider:
            return Decision(
                kind=kind,
                name=name,
                status="blocked",
                reason=(
                    "provider mismatch: cannot change provider from "
                    f"{current_provider!r} to {desired_provider!r} via apply"
                ),
                desired_hash=desired_hash,
                current_hash=canonical_hash(_CURRENT_VIEWS[kind](current)),
            )
    current_view = _CURRENT_VIEWS[kind](current)
    current_hash = canonical_hash(current_view)
    status = "unchanged" if current_hash == desired_hash else "updated"
    return Decision(
        kind=kind,
        name=name,
        status=status,
        desired_hash=desired_hash,
        current_hash=current_hash,
    )


def build_plan(
    desired_entities: dict[str, list[tuple[str, dict[str, Any]]]],
    current_entities: dict[str, dict[str, dict[str, Any]]],
    blocked_entities: list[tuple[str, str, str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the plan report over all desired entities.

    Parameters
    ----------
    desired_entities:
        kind -> [(name, managed_view)]
    current_entities:
        kind -> {name: raw current entity map}
    blocked_entities:
        optional pre-resolve failures as (kind, name, reason) tuples.
    """
    report: dict[str, list[dict[str, Any]]] = {
        "created": [],
        "updated": [],
        "unchanged": [],
        "blocked": [],
    }
    if blocked_entities:
        report["blocked"] = [{"kind": kind, "name": name, "reason": reason} for kind, name, reason in blocked_entities]
    for kind, entities in desired_entities.items():
        current_maps = current_entities.get(kind, {})
        for name, desired_view in entities:
            decision = plan_entity(kind, name, desired_view, current_maps.get(name))
            entry = (
                {"kind": kind, "name": name}
                if decision.status != "blocked"
                else {"kind": kind, "name": name, "reason": decision.reason}
            )
            report[decision.status].append(entry)
    return report


def has_blockers(report: dict[str, list[dict[str, Any]]]) -> bool:
    """True when the report contains blocked or failed entities."""
    return bool(report.get("blocked")) or bool(report.get("failed"))
