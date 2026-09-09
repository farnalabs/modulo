"""Trigger entity resolution + execution for ``modulo apply`` (FAR-681 slice 2).

Identity is (pipeline, name); the composite ``pipeline/name`` key appears in
plan reports. The pipeline is cross-referenced by NAME — resolvable when it
is declared in the same (or an earlier) config document or already present in
the org; forward references (a pipeline declared only in a LATER document)
are rejected at load/merge time by ApplyConfig.merge_entities, and missing
pipelines block the trigger at plan time with a reorder/apply-first hint.

config_json env refs (``${env:VAR}``) are resolved client-side at apply time;
``secretref://`` values are blocked (no server-side resolution yet — the same
policy as backend api_key in slice 1). Secret-shaped entries are excluded
from drift hashing on both sides (the server masks them on read and
Fernet-encrypts hmac_secret/signing_secret on write), while every
create/update re-sends the declared config so secret changes propagate with
any managed-field change.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

import httpx

from modulo.cli.apply.executor import _SECRETREF_BLOCK_REASON, ApplyHttpError, _failure_message, _find
from modulo.cli.apply.models import ENV_REF_PATTERN, TriggerEntity
from modulo.cli.apply.plan import KIND_PIPELINE, KIND_TRIGGER

if TYPE_CHECKING:
    from modulo.cli.apply.executor import ApplyExecutor
    from modulo.cli.apply.models import EntitySet

_log = logging.getLogger(__name__)


def resolve_trigger_config_refs(
    entities: dict[str, list[tuple[str, Any]]],
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str, str]]]:
    """Resolve ``${env:VAR}`` refs inside every trigger's config_json.

    Returns (resolved_config_by_composite_key, blocked) where blocked entries
    are (kind, composite-name, reason) for missing/empty env vars and for
    secretref:// values.
    """
    env: dict[str, str] = dict(environ if environ is not None else os.environ)
    resolved: dict[str, dict[str, Any]] = {}
    blocked: list[tuple[str, str, str]] = []
    for composite_key, entity in entities[KIND_TRIGGER]:
        assert isinstance(entity, TriggerEntity)
        out, failure = _resolve_config_json(entity.config_json, env)
        if failure is not None:
            blocked.append((KIND_TRIGGER, composite_key, failure))
        else:
            resolved[composite_key] = out
    return resolved, blocked


def _resolve_config_json(
    config: dict[str, Any],
    env: dict[str, str],
) -> tuple[dict[str, Any], str | None]:
    """Resolve env refs through a config tree; returns (resolved, failure)."""
    out: dict[str, Any] = {}
    for key, value in config.items():
        resolved_value, failure = _resolve_value(value, env)
        if failure is not None:
            return {}, failure
        out[key] = resolved_value
    return out, None


def _resolve_value(value: Any, env: dict[str, str]) -> tuple[Any, str | None]:
    if isinstance(value, str):
        match = ENV_REF_PATTERN.fullmatch(value)
        if match is not None:
            var = match.group(1)
            resolved = env.get(var)
            if resolved is None:
                return None, f"unresolved env ref: ${{env:{var}}} is not set"
            if not resolved.strip():
                return None, f"unresolved env ref: ${{env:{var}}} resolves to an empty value"
            return resolved, None
        if value.startswith("secretref://"):
            return None, _SECRETREF_BLOCK_REASON
        return value, None
    if isinstance(value, dict):
        return _resolve_config_json(value, env)
    if isinstance(value, list):
        resolved_items: list[Any] = []
        for item in value:
            resolved_item, failure = _resolve_value(item, env)
            if failure is not None:
                return None, failure
            resolved_items.append(resolved_item)
        return resolved_items, None
    return value, None


def build_desired_views(
    entity_set: EntitySet,
    current_entities: dict[str, dict[str, dict[str, Any]]],
    desired: dict[str, list[tuple[str, dict[str, Any]]]],
    blocked: list[tuple[str, str, str]],
    blocked_keys: set[tuple[str, str]],
) -> tuple[dict[str, list[tuple[str, dict[str, Any]]]], list[tuple[str, str, str]]]:
    """Plan-phase resolution for trigger entities (pipeline cross-refs).

    A trigger whose pipeline is neither declared in the config nor present in
    the org is blocked with a hint. A pipeline declared in a LATER document
    never reaches here (merge_entities rejects the forward reference at load).
    """
    current_pipelines = current_entities.get(KIND_PIPELINE) or {}
    declared_pipelines = {p.name for p in entity_set.pipelines}
    for entity in entity_set.triggers:
        composite_key = entity.display_key()
        if (KIND_TRIGGER, composite_key) in blocked_keys:
            continue
        if entity.pipeline not in current_pipelines and entity.pipeline not in declared_pipelines:
            blocked.append(
                (
                    KIND_TRIGGER,
                    composite_key,
                    (
                        f"pipeline {entity.pipeline!r} not found - declare it in this config (same or an "
                        "earlier document) or apply the pipeline first"
                    ),
                )
            )
            continue
        desired[KIND_TRIGGER].append((composite_key, entity.managed_view()))
    return desired, blocked


def apply_triggers(
    executor: ApplyExecutor,
    entities: dict[str, list[tuple[str, Any]]],
    current_entities: dict[str, dict[str, dict[str, Any]]],
    report: dict[str, list[dict[str, Any]]],
    pipeline_ids: dict[str, str],
    resolved_trigger_configs: dict[str, dict[str, Any]],
) -> None:
    """Create/update triggers per the plan report, capturing failures.

    Create: POST /pipelines/{pipeline_id}/triggers (TriggerCreate). Update:
    PUT /triggers/{trigger_id} (TriggerUpdate). Server-side ongoing-config
    validation rejections (validate_ongoing_config) surface as failed
    entities carrying the server's reason.
    """
    for status in ("created", "updated"):
        for entry in list(report[status]):
            if entry["kind"] != KIND_TRIGGER:
                continue
            entity = None
            try:
                entity = _find(entities[KIND_TRIGGER], entry["name"])
                assert isinstance(entity, TriggerEntity)
                pipeline_id = pipeline_ids.get(entity.pipeline)
                if pipeline_id is None:
                    msg = (
                        f"pipeline {entity.pipeline!r} not found - declare it in this config (same or an "
                        "earlier document) or apply the pipeline first"
                    )
                    raise KeyError(msg)
                resolved_config = resolved_trigger_configs.get(entry["name"])
                if resolved_config is None:
                    resolved_config, failure = _resolve_config_json(entity.config_json, dict(os.environ))
                    if failure is not None:
                        raise ValueError(failure)
                if status == "created":
                    executor._post(f"/pipelines/{pipeline_id}/triggers", entity.create_payload(resolved_config))
                else:
                    current = current_entities.get(KIND_TRIGGER) or {}
                    trigger_row = current[entry["name"]]
                    executor._put(f"/triggers/{trigger_row['id']}", entity.update_payload(resolved_config))
            except (ApplyHttpError, httpx.HTTPError, KeyError, ValueError) as exc:
                message = _failure_message(exc)
                displayed_name = entity.display_key() if entity else entry["name"]
                _log.warning("apply trigger failed: %s: %s", displayed_name, message)
                report[status].remove(entry)
                report["failed"].append({"kind": KIND_TRIGGER, "name": entry["name"], "error": message})


__all__ = [
    "apply_triggers",
    "build_desired_views",
    "resolve_trigger_config_refs",
]
