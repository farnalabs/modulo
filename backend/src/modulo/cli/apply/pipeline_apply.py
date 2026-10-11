"""Pipeline entity resolution + execution for ``modulo apply`` (FAR-681 slice 2).

Agent name-refs: config graph nodes reference agents by NAME; the executor
resolves names to ids via the fetched /agents map and BLOCKS the entity when
a referenced agent is missing (unmanaged reference — apply never auto-creates
agents). The resolved graph payload is normalised through the REAL API node /
edge models (the same validators the server runs on save) so the desired
canonical view matches the server's read-back byte-for-byte and the plan hash
is stable across runs.

Owner refs (FAR-1161): config pipelines reference accountability owners by
EMAIL; the executor resolves emails to account ids via the org member
directory (``/admin/users``, fetched lazily only when an owner email is
declared). An email that does not resolve — or a directory the credential
cannot read — BLOCKS the pipeline at plan time with a specific reason; a
declared owner is never silently nulled.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from modulo.cli.apply.executor import ApplyHttpError, _failure_message, _find
from modulo.cli.apply.models import (
    ApplyEntityResolutionError,
    PipelineEntity,
    graph_declares_secrets,
    strip_secret_shaped_graph,
)
from modulo.cli.apply.plan import KIND_PIPELINE, canonical_hash

if TYPE_CHECKING:
    from modulo.cli.apply.executor import ApplyExecutor
    from modulo.cli.apply.models import EntitySet
    from modulo.core.pipeline_engine.git_content import GitContentRef

_log = logging.getLogger(__name__)

_EMPTY_GRAPH: dict[str, list[dict[str, Any]]] = {"nodes": [], "edges": []}


def _normalized_node(node_data: dict[str, Any]) -> dict[str, Any]:
    """Config node dict -> server-shape dump (real API node model validators)."""
    from modulo.api.routes.pipelines import PipelineGraphNode

    return PipelineGraphNode.model_validate(node_data).model_dump(mode="json")


def _normalized_edge(edge_data: dict[str, Any]) -> dict[str, Any]:
    """Config edge dict -> server-shape dump (real API edge model validators)."""
    from modulo.api.routes.pipelines import PipelineGraphEdge

    return PipelineGraphEdge.model_validate(edge_data).model_dump(mode="json")


def normalize_current_graph(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise a fetched /pipelines/{id}/graph payload to the hash shape.

    Validated with the API models using the READ context (legacy_read) so a
    legacy stored gate description does not fail the fetch — the read path on
    the server uses the same context.  Nodes also use the read context so that
    legacy nodes the API read tolerates do not fail the CLI apply/drift path.
    The hash-parity guarantee holds because the desired side is normalised
    through the same models (write context).
    """
    from modulo.api.routes.pipelines import LEGACY_READ_CONTEXT, PipelineGraphEdge, PipelineGraphNode

    return {
        "nodes": [
            PipelineGraphNode.model_validate(node, context=LEGACY_READ_CONTEXT).model_dump(mode="json")
            for node in payload.get("nodes") or []
        ],
        "edges": [
            PipelineGraphEdge.model_validate(edge, context=LEGACY_READ_CONTEXT).model_dump(mode="json")
            for edge in payload.get("edges") or []
        ],
    }


def resolve_graph(
    entity: PipelineEntity,
    agent_ids: dict[str, Any],
    *,
    git_resolver: Callable[[GitContentRef], str] | None = None,
) -> dict[str, Any]:
    """Resolve agent name-refs + git content refs, then normalise the declared graph.

    FAR-220: every ``git+<repo>[@<ref>]#<path>`` content ref (``agent_prompt``,
    ``script_command``, ``agent_commands`` items) is resolved to its pinned
    commit SHA — an already-pinned ref is re-canonicalised without network
    access; a movable ref (branch/tag/HEAD) resolves through *git_resolver*
    (default: ``git ls-remote``). The graph payload therefore carries the
    PINNED form, so the desired managed view, the write payload, the stored
    graph, and every run snapshot all hold the same resolved SHA (drift
    compares SHAs, not moving branch names).

    Raises ApplyEntityResolutionError when a referenced agent is missing or a
    git content ref is malformed/unresolvable (unmanaged reference / failed
    resolution: the entity is blocked, never silently written unpinned), or
    ValueError (pydantic ValidationError included) when the resolved payload
    fails the API node/edge shape rules.
    """
    from modulo.core.pipeline_engine.git_content import (
        GitContentRefError,
        pin_git_content_node_fields,
    )

    declared = entity.graph
    nodes_payload: list[dict[str, Any]] = []
    for node in declared.nodes if declared is not None else []:
        node_data = node.api_node_payload()
        if node.agent is not None:
            agent_id = agent_ids.get(node.agent)
            if agent_id is None:
                msg = (
                    f"agent {node.agent!r} not found in target org - declare the agent in this config "
                    "or create it first; apply never auto-creates agents"
                )
                raise ApplyEntityResolutionError(msg)
            node_data["agent_id"] = agent_id
        try:
            pin_git_content_node_fields(node_data, resolver=git_resolver)
        except GitContentRefError as exc:
            msg = f"git content ref for node {node.id!s}: {exc}"
            raise ApplyEntityResolutionError(msg) from None
        nodes_payload.append(_normalized_node(node_data))
    edges_payload = [
        _normalized_edge(edge.api_edge_payload()) for edge in (declared.edges if declared is not None else [])
    ]
    return {"nodes": nodes_payload, "edges": edges_payload}


def resolve_owner_ids(
    entity: PipelineEntity,
    current_entities: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Resolve the entity's declared owner emails to account ids (FAR-1161).

    Returns ``(business_owner_id, reliability_owner_id)`` — None where no
    email is declared (declarative omission means null). Raises
    ApplyEntityResolutionError (loud, caller blocks/fails the entity) when a
    declared email does not resolve to an ACTIVE member of the org, or when
    the member directory could not be read at all (``users_error``) — a
    declared owner is never silently nulled.
    """
    users_error = current_entities.get("users_error")
    declared = entity.business_owner_email or entity.reliability_owner_email
    if users_error and declared:
        raise ApplyEntityResolutionError(f"cannot resolve owner emails: {users_error}")
    users_map = current_entities.get("users") or {}
    resolved: list[str | None] = []
    for field, email in (
        ("business_owner_email", entity.business_owner_email),
        ("reliability_owner_email", entity.reliability_owner_email),
    ):
        if not email:
            resolved.append(None)
            continue
        owner_id = users_map.get(str(email).strip().lower())
        if owner_id is None:
            raise ApplyEntityResolutionError(
                f"{field} {email!r} does not resolve to an active member of this organisation"
            )
        resolved.append(str(owner_id))
    return resolved[0], resolved[1]


def build_desired_views(
    entity_set: EntitySet,
    current_entities: dict[str, dict[str, dict[str, Any]]],
    desired: dict[str, list[tuple[str, dict[str, Any]]]],
    blocked: list[tuple[str, str, str]],
    blocked_keys: set[tuple[str, str]],
    *,
    git_resolver: Callable[[GitContentRef], str] | None = None,
    refresh_secrets: bool = False,
) -> tuple[dict[str, list[tuple[str, dict[str, Any]]]], list[tuple[str, str, str]]]:
    """Plan-phase resolution for pipeline entities (agent refs + git content refs + owner emails + shape).

    Failures are per-entity blocked entries; later entities still plan. An
    AMBIGUOUS name (duplicate rows in the fetched org list — pipelines or
    agents) blocks the affected entity: name-based upsert cannot pick a row.
    *git_resolver* threads into :func:`resolve_graph` for FAR-220 movable
    ``git+`` refs (None = the default ``git ls-remote`` resolver).
    An unresolvable declared owner email blocks the affected entity (FAR-1161).

    With ``refresh_secrets`` set, a pipeline whose RESOLVED graph declares
    secret-shaped entries (FAR-1232: ``graph_declares_secrets`` — entries
    the drift hash redacts because the API read path masks them) is marked
    with a ``secrets_refresh`` view flag so the plan always reports
    ``updated`` and the write path re-sends the declared true graph —
    secret-only graph rotation is invisible to the hash otherwise.
    """
    agents = current_entities.get("agents") or {}
    ambiguous_pipelines = current_entities.get("ambiguous_pipeline_names") or {}
    ambiguous_agents = current_entities.get("ambiguous_agent_names") or {}
    for entity in entity_set.pipelines:
        if (KIND_PIPELINE, entity.name) in blocked_keys:
            continue
        pipeline_matches = ambiguous_pipelines.get(entity.name)
        if pipeline_matches is not None:
            blocked.append(
                (KIND_PIPELINE, entity.name, f"pipeline {entity.name!r}: ambiguous name, {pipeline_matches} matches")
            )
            continue
        agent_conflict = next(
            (
                node.agent
                for node in (entity.graph.nodes if entity.graph is not None else [])
                if node.agent is not None and node.agent in ambiguous_agents
            ),
            None,
        )
        if agent_conflict is not None:
            blocked.append(
                (
                    KIND_PIPELINE,
                    entity.name,
                    f"agent {agent_conflict!r}: ambiguous name, {ambiguous_agents[agent_conflict]} matches",
                )
            )
            continue
        try:
            graph = resolve_graph(entity, agents, git_resolver=git_resolver) if entity.graph is not None else None
        except ApplyEntityResolutionError as exc:
            blocked.append((KIND_PIPELINE, entity.name, str(exc)))
            continue
        except ValueError as exc:
            blocked.append((KIND_PIPELINE, entity.name, f"invalid pipeline graph: {exc}"))
            continue
        try:
            business_owner_id, reliability_owner_id = resolve_owner_ids(entity, current_entities)
        except ApplyEntityResolutionError as exc:
            blocked.append((KIND_PIPELINE, entity.name, str(exc)))
            continue
        desired[KIND_PIPELINE].append(
            (
                entity.name,
                entity.managed_view(
                    graph=graph,
                    business_owner_id=business_owner_id,
                    reliability_owner_id=reliability_owner_id,
                ),
            )
        )
        # FAR-1232 --refresh-secrets: a graph declaring secret-shaped entries
        # is always re-sent (the criterion runs on the TRUE resolved graph,
        # never its masked hash view). The marker key participates in the
        # canonical hash (the current view cannot produce it), so the plan
        # reports updated for the refreshed run.
        if refresh_secrets and graph is not None and graph_declares_secrets(graph):
            desired[KIND_PIPELINE][-1][1]["secrets_refresh"] = True
    return desired, blocked


def _graph_write_required(
    graph: dict[str, Any] | None,
    current_graph: dict[str, Any],
    *,
    refresh_secrets: bool,
) -> bool:
    """FAR-1232: decide whether the declared graph must be re-sent on PATCH.

    Hashes BOTH sides through the same symmetric redaction the plan hash
    used (``strip_secret_shaped_graph``) — comparing the raw declaration
    against the API-masked read would re-send the graph (and churn a
    snapshot) on every updated-pipeline run: a masked placeholder means
    "unchanged" on the same instance and is never re-sent as a literal.
    With ``refresh_secrets``, a secret-declaring graph is re-sent outright
    (the TRUE declared values write through; the masked read is never the
    write payload).
    """
    if graph is None:
        return False
    if canonical_hash(strip_secret_shaped_graph(graph)) != canonical_hash(strip_secret_shaped_graph(current_graph)):
        return True
    return bool(refresh_secrets and graph_declares_secrets(graph))


def _create_payload(
    entity: PipelineEntity,
    business_owner_id: str | None,
    reliability_owner_id: str | None,
) -> dict[str, Any]:
    """Build the POST /pipelines body for a declared pipeline."""
    payload: dict[str, Any] = {
        "name": entity.name,
        "description": entity.description,
        "max_concurrent_runs": entity.max_concurrent_runs,
        # Always sent: managed_view unconditionally compares this key, so the
        # create path must carry the declared value (explicit null = no
        # override).
        "stdout_retention_config": entity.stdout_retention_config,
        # Always sent (incl. explicit null): managed unconditionally like
        # stdout_retention_config — omission means null (inherit
        # org/instance default).
        "hitl_review_window_seconds": entity.hitl_review_window_seconds,
        "max_autonomy_level": entity.max_autonomy_level,
        "business_owner_id": business_owner_id,
        "reliability_owner_id": reliability_owner_id,
    }
    if entity.manages_circuit_breaker:
        payload["circuit_breaker_threshold"] = entity.circuit_breaker_threshold
    # FAR-1294: the two runtime limits are sent only when the config
    # DECLARES them, so an omitted key falls through to the server default
    # (300 / 3600) instead of being written as an explicit null (which
    # PipelineCreate would reject).
    if entity.manages_node_timeout:
        payload["node_timeout_seconds"] = entity.node_timeout_seconds
    if entity.manages_max_duration:
        payload["max_duration_seconds"] = entity.max_duration_seconds
    return payload


def _patch_payload(
    entity: PipelineEntity,
    status: str,
    *,
    business_owner_id: str | None,
    reliability_owner_id: str | None,
    graph: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the PATCH /pipelines/{id} body for a declared pipeline.

    *graph* is the resolved graph to write, or None when the graph must not
    be re-sent (undeclared or unchanged — a graph write snapshots the
    pipeline, so an unchanged graph must not churn snapshots).
    """
    payload: dict[str, Any] = {
        "description": entity.description,
        "max_concurrent_runs": entity.max_concurrent_runs,
        # Always sent (including explicit null): the drift hash compares this
        # key unconditionally, so the write path must be able to set AND
        # clear it (PipelineUpdate accepts null; the PATCH route uses
        # exclude_unset so an explicit null clears the stored override).
        "stdout_retention_config": entity.stdout_retention_config,
        # Always sent (including explicit null): managed unconditionally like
        # stdout_retention_config, so the declared null must be able to CLEAR
        # a UI/API-set per-pipeline override (PipelineUpdate accepts null).
        "hitl_review_window_seconds": entity.hitl_review_window_seconds,
        "business_owner_id": business_owner_id,
        "reliability_owner_id": reliability_owner_id,
    }
    if status == "updated" and entity.manages_circuit_breaker:
        payload["circuit_breaker_threshold"] = entity.circuit_breaker_threshold
    # FAR-1163/FAR-1221: send the ceiling only when DECLARED — an omitted
    # ceiling must never clear a UI-set one as a side effect (mirrors the
    # managed-view omission above), while a DECLARED null sends an explicit
    # null so the REST PATCH clears the column back to "inherit the
    # default".
    if entity.manages_max_autonomy:
        payload["max_autonomy_level"] = entity.max_autonomy_level
    # FAR-1599: the environment-profile binding is sent only when DECLARED
    # (opt-in like the ceiling above): a DECLARED id binds, a DECLARED null
    # clears the binding, and an omitted key is never sent so a UI/API-set
    # binding survives untouched. PipelineCreate does not accept the field,
    # so a CREATED pipeline binds through this PATCH (the caller force-sends
    # it when the config declares the key, even with no graph) — the server
    # validates the binding with the shared FAR-1558 predicate and a
    # refusal fails the entity loudly.
    if entity.manages_environment_profile:
        # Stringify: the field parses to a uuid.UUID, but the wire payload
        # is JSON — send the id in the API's string space (None serialises
        # as the clearing null).
        payload["environment_profile_id"] = (
            None if entity.environment_profile_id is None else str(entity.environment_profile_id)
        )
    # FAR-1294: same opt-in gate as the create path. A DECLARED limit is
    # always sent as a concrete int (the drift hash compares the key, so the
    # write must be able to change it); an OMITTED limit is never sent, so a
    # UI/API-tuned timeout or duration cap survives an apply that does not
    # declare it.
    if entity.manages_node_timeout:
        payload["node_timeout_seconds"] = entity.node_timeout_seconds
    if entity.manages_max_duration:
        payload["max_duration_seconds"] = entity.max_duration_seconds
    if graph is not None:
        payload["graph_json"] = graph
    return payload


def _apply_one_pipeline(
    executor: ApplyExecutor,
    entity: PipelineEntity,
    status: str,
    *,
    agents: dict[str, Any],
    current_pipelines: dict[str, dict[str, Any]],
    current_entities: dict[str, dict[str, dict[str, Any]]],
    refresh_secrets: bool,
    git_resolver: Callable[[GitContentRef], str] | None,
) -> str:
    """Create or update ONE pipeline; returns its pipeline id.

    Raises ApplyHttpError / httpx.HTTPError / KeyError / ValueError on
    failure — the caller isolates each pipeline in its own try/except so one
    bad pipeline never aborts the run.

    Create: POST /pipelines, then a graph PATCH (PATCH /{id} graph_json)
    when the config declares a graph. Update: PATCH /{id} with the managed
    top-level fields; graph_json is included only when the graph hash
    differs from the fetched current graph (decided by
    :func:`_graph_write_required`), with ``refresh_secrets`` re-sending
    secret-declaring graphs outright (true declared values, never the
    masked read).
    """
    graph = resolve_graph(entity, agents, git_resolver=git_resolver) if entity.graph is not None else None
    business_owner_id, reliability_owner_id = resolve_owner_ids(entity, current_entities)
    if status == "created":
        response = executor._post("/pipelines", _create_payload(entity, business_owner_id, reliability_owner_id))
        pipeline_id = str(response["id"])
        # FAR-1530: a declared run_enabled (only ever false) pauses the
        # pipeline IMMEDIATELY after create — before the graph PATCH below,
        # so a later failure leaves the row paused (fail closed), never
        # silently running. The pause rides the dedicated /pause route (the
        # single transition surface with its audit + 409 traps), not PATCH.
        if entity.manages_run_enabled:
            executor._post(f"/pipelines/{pipeline_id}/pause")
        graph_differs = entity.graph is not None
    else:
        current = current_pipelines[entity.name]
        pipeline_id = str(current["id"])
        current_graph = current.get("graph") or _EMPTY_GRAPH
        graph_differs = _graph_write_required(graph, current_graph, refresh_secrets=refresh_secrets)
    patch_payload = _patch_payload(
        entity,
        status,
        business_owner_id=business_owner_id,
        reliability_owner_id=reliability_owner_id,
        graph=graph if graph_differs else None,
    )
    # FAR-1599: a create with a declared binding must PATCH even without a
    # graph — PipelineCreate cannot carry environment_profile_id, so the
    # bind rides the follow-up PATCH.
    if status == "updated" or entity.graph is not None or entity.manages_environment_profile:
        executor._patch(f"/pipelines/{pipeline_id}", patch_payload)
    # FAR-1530: converge a declared run_enabled (only ever false) by POSTing
    # the dedicated /pause route AFTER the field PATCH — pause is idempotent
    # server-side (first cause owns the reason), so this is safe when the
    # entry was marked updated for an unrelated key while the pipeline is
    # already paused. The PATCH never carries run_enabled: the pause/resume
    # routes are the single transition surface (audit + 409 traps live
    # there), and config can never express a resume.
    if status == "updated" and entity.manages_run_enabled:
        executor._post(f"/pipelines/{pipeline_id}/pause")
    return pipeline_id


def _record_pipeline_failure(
    report: dict[str, list[dict[str, Any]]],
    status: str,
    entry: dict[str, Any],
    entity: PipelineEntity | None,
    exc: Exception,
    pipeline_ids: dict[str, str],
) -> None:
    """Move a failed pipeline entry to ``report['failed']`` (isolated per pipeline).

    A JUST-CREATED pipeline whose apply failed must not hand dependent
    triggers an id that points at a graph-less half-created row, so its id
    is dropped from *pipeline_ids*.
    """
    message = _failure_message(exc)
    displayed_name = entity.name if entity else entry["name"]
    _log.warning("apply pipeline failed: %s: %s", displayed_name, message)
    report[status].remove(entry)
    report["failed"].append({"kind": KIND_PIPELINE, "name": entry["name"], "error": message})
    if status == "created" and entity is not None:
        pipeline_ids.pop(entity.name, None)


def apply_pipelines(
    executor: ApplyExecutor,
    entities: dict[str, list[tuple[str, Any]]],
    current_entities: dict[str, dict[str, dict[str, Any]]],
    report: dict[str, list[dict[str, Any]]],
    *,
    git_resolver: Callable[[GitContentRef], str] | None = None,
    refresh_secrets: bool = False,
) -> dict[str, str]:
    """Create/update pipelines per the plan report, capturing failures.

    Per-pipeline semantics (payload contents, FAR-1232 graph-write
    decision, FAR-1530 pause routing) live in :func:`_apply_one_pipeline`;
    this orchestrator owns phase order and failure isolation.

    The accountability-owner keys (FAR-1161) are ALWAYS sent on both POST
    and PATCH: the resolved id, or null when the config declares no owner
    (declarative omission means null — an explicit null clears a UI/API-set
    owner; the REST PATCH keys on presence, so omitting them would wrongly
    leave the live owner untouched).

    The runtime limits ``node_timeout_seconds`` / ``max_duration_seconds``
    (FAR-1294) are the opposite: sent on both POST and PATCH only when the
    config declares them, because the REST models accept no null for either —
    an omitted key must leave a UI/API-tuned value untouched instead of
    failing the entity with a 422 (or, on create, fall through to the server
    default of 300 / 3600).

    ``environment_profile_id`` (FAR-1599) is sent on PATCH only when DECLARED
    (opt-in): a declared id binds, a declared null clears, an omitted key is
    never sent. ``PipelineCreate`` has no such field, so a CREATED pipeline
    binds through the follow-up PATCH (force-sent when the key is declared,
    even without a graph). The server validates every write with the shared
    FAR-1558 predicate; a refusal fails the entity with the API's message.

    Returns the pipeline name -> id map (current + created), consumed by the
    trigger phase to resolve (pipeline, name) identities. When a JUST-CREATED
    pipeline's apply fails (POST or the graph PATCH), the name is dropped
    from the map so dependent triggers fail with "pipeline apply failed
    upstream" instead of applying against a graph-less half-created row.
    """
    agents = current_entities.get("agents") or {}
    current_pipelines = current_entities.get(KIND_PIPELINE) or {}
    pipeline_ids: dict[str, str] = {
        name: str(item["id"]) for name, item in current_pipelines.items() if item.get("id") is not None
    }
    for status in ("created", "updated"):
        for entry in list(report[status]):
            if entry["kind"] != KIND_PIPELINE:
                continue
            entity: PipelineEntity | None = None
            try:
                entity = _find(entities[KIND_PIPELINE], entry["name"])
                assert isinstance(entity, PipelineEntity)
                pipeline_ids[entity.name] = _apply_one_pipeline(
                    executor,
                    entity,
                    status,
                    agents=agents,
                    current_pipelines=current_pipelines,
                    current_entities=current_entities,
                    refresh_secrets=refresh_secrets,
                    git_resolver=git_resolver,
                )
            except (ApplyHttpError, httpx.HTTPError, KeyError, ValueError) as exc:
                _record_pipeline_failure(report, status, entry, entity, exc, pipeline_ids)
    return pipeline_ids


__all__ = [
    "apply_pipelines",
    "build_desired_views",
    "normalize_current_graph",
    "resolve_graph",
    "resolve_owner_ids",
]
