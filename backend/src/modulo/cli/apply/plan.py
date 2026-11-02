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
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)


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
KIND_PIPELINE = "pipeline"
KIND_TRIGGER = "trigger"
KIND_SYSTEM_MAP = "system_map"


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


def _effective_allow_insecure_http(base_url: Any) -> bool:
    """Is the live endpoint plain ``http``? (the server-observable opt-in form)."""
    return isinstance(base_url, str) and base_url.lower().startswith("http://")


def _backend_managed_current_view(
    current: dict[str, Any],
    desired_view: dict[str, Any],
) -> dict[str, Any]:
    """Current view restricted to the DESIRED managed keys (FAR-2105).

    The endpoint fields are managed ONLY when the config declares them, so a
    UI/API-set ``base_url`` must not surface as drift against a config that
    never mentions it — the current view is therefore restricted to the desired
    key set (the ``env_profile_id`` / owner precedent, extended to a kind whose
    baseline view was previously unconditional).

    ``base_url`` compares verbatim (the API returns the trimmed stored value,
    and the declared value is trimmed at load). ``allow_insecure_http`` has no
    stored column and is not echoed by the API: it is a validation gate whose
    persisted evidence is the endpoint's scheme, so the live side derives the
    SAME observable value the desired side hashes
    (``ModelBackendEntity.effective_allow_insecure_http``). A raw-flag
    comparison would report permanent false drift for ``https`` +
    ``allow_insecure_http``.
    """
    view: dict[str, Any] = {}
    for key in desired_view:
        if key == "base_url":
            view[key] = current.get(key)
        elif key == "allow_insecure_http":
            view[key] = _effective_allow_insecure_http(current.get("base_url"))
        elif key == "default_params":
            view[key] = current.get(key) or {}
        else:
            view[key] = current.get(key)
    return view


def _pipeline_current_view(current: dict[str, Any], desired_view: dict[str, Any]) -> dict[str, Any]:
    """Current view restricted to the DESIRED managed keys.

    A graph-less config does not manage the graph at all (the key is absent
    from the desired view), so a UI-authored graph never causes drift.
    FAR-1161: ``business_owner_id`` / ``reliability_owner_id`` are part of the
    canonical-hash input (the desired view always carries both keys — see
    ``PipelineEntity.managed_view``) so ``--diff`` detects owner drift.
    FAR-1232: the current graph is REDACTED through the server mask tiers the
    desired side is redacted with (``strip_secret_shaped_graph`` — mirror of
    ``mask_pipeline_graph_node``, which masks credential-bearing node fields
    on read); re-redaction is idempotent, so the two sides hash equal whenever
    the declaration matches stored state — no phantom 'updated' plan.
    """
    from modulo.cli.apply.models import (
        quantize_circuit_breaker_threshold,
        strip_secret_shaped_graph,
    )

    view: dict[str, Any] = {}
    for key in desired_view:
        if key == "graph":
            raw_current = current.get("graph") or {"nodes": [], "edges": []}
            view[key] = strip_secret_shaped_graph(raw_current)
        elif key == "circuit_breaker_threshold":
            # The API serialises the Numeric(14, 6) column as a float;
            # canonicalise both sides to 6dp so no false drift is reported.
            view[key] = quantize_circuit_breaker_threshold(current.get(key))
        elif key in ("environment_profile_id", "business_owner_id", "reliability_owner_id"):
            # UUID-backed bindings use the same string representation on both
            # sides, including when a test double supplies a UUID object.
            value = current.get(key)
            view[key] = None if value is None else str(value)
        else:
            view[key] = current.get(key)
    return view


def _trigger_current_view(current: dict[str, Any], desired_view: dict[str, Any]) -> dict[str, Any]:
    """Current view restricted to the desired managed keys.

    config_json is compared on the DESIRED key set only (apply merges its
    declared keys and never deletes foreign keys, so undeclared stored keys
    are unmanaged) with secret-shaped entries stripped symmetrically with the
    desired side (the API masks server-stored secrets on read).
    daily_spend_limit is canonicalised to the column's 4dp scale on both
    sides (the column is Numeric(12, 4); the API serialises it as float) so a
    higher-precision declaration cannot produce permanent false drift.
    """
    from modulo.cli.apply.models import quantize_daily_spend_limit, strip_secret_shaped_config

    view: dict[str, Any] = {}
    for key, desired_value in desired_view.items():
        current_value = current.get(key)
        if key == "daily_spend_limit":
            current_value = quantize_daily_spend_limit(current_value)
        elif key == "config_json" and isinstance(desired_value, dict):
            current_config = current_value if isinstance(current_value, dict) else {}
            restricted = {k: current_config[k] for k in desired_value if k in current_config}
            current_value = strip_secret_shaped_config(restricted)
        view[key] = current_value
    return view


def _system_map_managed_desired_view(desired_view: dict[str, Any]) -> dict[str, Any]:
    """Desired system-map view without the create-only revision block.

    ``modulo apply`` creates a map's declared revision only on CREATE. The
    system-map list endpoint exposes only map metadata (``published_revision_id``
    — never revision content), so an existing map can never report its revision
    back. Hashing the declared revision against every existing map classified
    it ``updated`` on every run and never converged. Revision content is
    therefore outside the managed comparison for existing maps, and a declared
    revision on an existing map is surfaced as an explicit warning by
    :func:`plan_entity` rather than being silently ignored.
    """
    return {key: value for key, value in desired_view.items() if key != "revision"}


def _system_map_current_view(current: dict[str, Any]) -> dict[str, Any]:
    """Current view for system maps - map metadata only.

    Revision content is create-only (see
    :func:`_system_map_managed_desired_view`); only "draft" status is managed
    by apply, and publish is an explicit API action rather than an implicit
    state transition.
    """
    view: dict[str, Any] = {}
    for key in ("description", "owner_team_id", "visibility", "default_map"):
        view[key] = current.get(key)
    return view


_CURRENT_VIEWS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    KIND_SCHEMA: _schema_current_view,
    KIND_BACKEND: _backend_current_view,
    KIND_SYSTEM_MAP: _system_map_current_view,
}


def _version_conflict(
    desired_view: dict[str, Any],
    current: dict[str, Any],
) -> str | None:
    """Return a blocked reason when a declared version string exists with
    different content, else None.

    Schema versions are immutable via apply: a same-version-different-content
    declaration can never converge (the executor skips existing version
    strings), so it must be blocked at plan time instead of silently ignored.
    """
    declared = {v["version"]: v for v in (desired_view.get("versions") or [])}
    if not declared:
        return None
    for existing in current.get("versions") or []:
        spec = declared.get(existing.get("version"))
        if spec is None:
            continue
        if (
            spec["version_number"] != existing.get("version_number")
            or spec["definition_json"] != existing.get("definition_json")
            or spec["published"] != existing.get("published")
        ):
            return (
                f"version {existing.get('version')} exists with different content "
                "- schema versions are immutable via apply"
            )
    return None


def _current_view(kind: str, current: dict[str, Any], desired_view: dict[str, Any]) -> dict[str, Any]:
    """Build the current managed view for one entity kind."""
    if kind == KIND_BACKEND:
        return _backend_managed_current_view(current, desired_view)
    if kind == KIND_PIPELINE:
        return _pipeline_current_view(current, desired_view)
    if kind == KIND_TRIGGER:
        return _trigger_current_view(current, desired_view)
    if kind == KIND_SYSTEM_MAP:
        return _system_map_current_view(current)
    return _CURRENT_VIEWS[kind](current)


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
    desired_hash_view = desired_view
    if kind == KIND_SYSTEM_MAP and current is not None:
        if "revision" in desired_view:
            _log.warning(
                "apply.system_map.revision_create_only: system map %r already exists; "
                "declared revision content is applied on create only and is not reconciled on update",
                name,
            )
        desired_hash_view = _system_map_managed_desired_view(desired_view)
    desired_hash = canonical_hash(desired_hash_view)
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
    if kind == KIND_SCHEMA:
        conflict = _version_conflict(desired_view, current)
        if conflict is not None:
            return Decision(
                kind=kind,
                name=name,
                status="blocked",
                reason=conflict,
                desired_hash=desired_hash,
                current_hash=canonical_hash(_CURRENT_VIEWS[kind](current)),
            )
    current_view = _current_view(kind, current, desired_view)
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
