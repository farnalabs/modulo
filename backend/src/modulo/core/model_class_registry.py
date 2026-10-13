"""ADR 037 rev 2 — product-side ``model_class`` resolution for sandbox_agent nodes.

**EXPERIMENTAL** (FAR-1743 / FAR-892 slice A): the ``model_class`` node field,
the ``{{ model }}`` template variable, and the ``_resolved_model`` /
``_model_origin`` snapshot metadata are all experimental until slice B's
canary + migration sweep proves them out; every surface may change before it
is de-experimentalised.

A ``sandbox_agent`` node may declare an optional ``model_class`` — the name of
a registry **class** (a capability contract such as ``planner`` / ``builder`` /
``reviewer``), NOT a credential-bearing Model Backend entity (D1). This module
owns the product-side resolution seam (the registry file is devtools-owned; the
lookup is product-owned — the same split as ``connector_type``):

* :func:`resolve_graph_model_classes` runs **at snapshot mint** (the per-run
  seam that already bakes prompt/backend/guardrail pins) and bakes
  ``_resolved_model`` (the concrete model id) + ``_model_origin`` (which
  precedence rung supplied it) into each opted-in node's metadata, so every
  later render replays the SNAPSHOT, never the live registry.
* :func:`effective_model_template_value` is the **render-side** read: the
  top-level ``{{ model }}`` template variable, with the run's frozen
  ``_run_overrides.model`` folded in pre-dispatch (run override wins over the
  baked value). It never consults the registry and never mutates ``input``.

**Fail closed (D3).** On resolution failure — unknown class, missing registry,
unreadable or corrupt JSON — :class:`ModelClassResolutionError` rejects the
operation before dispatch (no sandbox, zero cost). The ONLY soft path is a
fallback the class itself declares in the registry (``classes.<name>.fallback``);
the resolver never invents an undeclared default.

Precedence (ADR 037 rev 2)::

    _run_overrides.model        (variant/run-time override — folded in at RENDER)
           ↓ absent
    model_class on node         → _model_origin: node_model_class
           ↓ absent
    registry pipeline/node map  → _model_origin: registry_pipeline_map
           ↓ absent
    registry pipeline default   → _model_origin: registry_pipeline_default

The org-level default rung from rev 1 is deliberately dropped (no speculative
configuration). Registry rungs only apply when a registry is *configured*
(``MODEL_CLASS_REGISTRY_PATH``); with no registry and no ``model_class``
anywhere, resolution is a no-op so every existing pipeline keeps working
unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

# `_model_origin` rung vocabulary (ADR 037 rev 2). The run-override rung never
# lands on the snapshot (it is folded in at render from the run's own frozen
# `_run_overrides`); it is named here so the vocabulary has one home.
MODEL_ORIGIN_RUN_OVERRIDE = "run_override"
MODEL_ORIGIN_NODE_MODEL_CLASS = "node_model_class"
MODEL_ORIGIN_REGISTRY_PIPELINE_MAP = "registry_pipeline_map"
MODEL_ORIGIN_REGISTRY_PIPELINE_DEFAULT = "registry_pipeline_default"


class ModelClassResolutionError(HTTPException):
    """Fail-closed rejection when a ``model_class`` cannot be resolved (D3).

    A 422 so every run-creation surface answers with a clear, structured error
    naming the node and the failure reason instead of an opaque 500: REST
    routes surface it through the normal ``StarletteHTTPException`` handler,
    the MCP tool classifier renders 4xx as the branchable ``validation_failed``
    code, and a background (cron/trigger) caller fails the job loudly with the
    same message. Subclassing ``HTTPException`` is what lets ONE exception type
    serve all three seams without per-route mapping — a domain exception would
    need handlers in every run-creation route (the sizing this ticket avoids).

    Raised from snapshot mint, so the run row is never created — the refusal
    is pre-dispatch, zero sandbox cost, zero model cost.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _read_registry(registry_path: str | None) -> dict[str, Any]:
    """Load and structurally validate the model-management registry JSON.

    Fail closed (D3) on every failure mode: path not configured, file missing
    or unreadable, malformed JSON, or a non-object / malformed top-level
    section. Only the sections this resolver reads are structurally validated
    (``classes`` and ``pipelines`` must be objects when present); a bad entry
    inside them is caught by the lookups below with a node-naming message.
    """
    if not registry_path:
        raise ModelClassResolutionError(
            "model_class resolution requires a model-class registry, but no registry path is "
            "configured (set MODEL_CLASS_REGISTRY_PATH to a model-management.json file)"
        )
    path = Path(registry_path)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelClassResolutionError(
            f"model-class registry '{registry_path}' is unreadable ({type(exc).__name__}: {exc}) — "
            "run refused pre-dispatch (ADR 037 D3)"
        ) from exc
    if not isinstance(data, dict):
        raise ModelClassResolutionError(
            f"model-class registry '{registry_path}' is corrupt: top level is not a JSON object"
        )
    for section in ("classes", "pipelines"):
        value = data.get(section)
        if value is not None and not isinstance(value, dict):
            raise ModelClassResolutionError(
                f"model-class registry '{registry_path}' is corrupt: '{section}' is not a JSON object"
            )
    return data


def _model_for_class(registry: dict[str, Any], class_name: str, node_id: Any) -> str:
    """Map a registry class to its concrete model id.

    The ONLY soft path (D3): when the class entry declares a ``fallback`` but
    no ``model``, the declared fallback is used — an explicit, opt-in per-class
    entry. An unknown class, a class with no usable ``model`` and no declared
    ``fallback``, or a malformed entry fails closed with a message naming the
    node.
    """
    classes = registry.get("classes") or {}
    entry = classes.get(class_name)
    if not isinstance(entry, dict):
        known = ", ".join(sorted(str(k) for k in classes)) or "<none>"
        raise ModelClassResolutionError(
            f"sandbox_agent node '{node_id}' declares model_class '{class_name}' which the "
            f"model-class registry does not define (known classes: {known}) — run refused "
            "pre-dispatch (ADR 037 D3)"
        )
    model = entry.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    fallback = entry.get("fallback")
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    raise ModelClassResolutionError(
        f"sandbox_agent node '{node_id}': model class '{class_name}' declares neither 'model' "
        "nor a 'fallback' — run refused pre-dispatch (ADR 037 D3)"
    )


def _pipeline_map_class(registry: dict[str, Any], pipeline_name: str, node_id: Any) -> str | None:
    """Rung 3 — the registry ``pipelines`` map entry for this pipeline/node.

    The entry is either a bare class string (applies to every node in the
    pipeline) or an object with an optional per-``nodes`` (node-id keyed)
    override and a ``default``. Returns ``None`` when the pipeline is not
    mapped; a mapped-but-malformed entry fails closed.
    """
    entry = (registry.get("pipelines") or {}).get(pipeline_name)
    if entry is None:
        return None
    if isinstance(entry, str):
        if entry.strip():
            return entry.strip()
        raise ModelClassResolutionError(
            f"model-class registry pipelines entry for '{pipeline_name}' is an empty class name "
            "— run refused pre-dispatch (ADR 037 D3)"
        )
    if not isinstance(entry, dict):
        raise ModelClassResolutionError(
            f"model-class registry pipelines entry for '{pipeline_name}' is malformed "
            f"({type(entry).__name__}) — run refused pre-dispatch (ADR 037 D3)"
        )
    nodes_map = entry.get("nodes")
    if nodes_map is not None:
        if not isinstance(nodes_map, dict):
            raise ModelClassResolutionError(
                f"model-class registry pipelines entry for '{pipeline_name}' has a malformed "
                "'nodes' map — run refused pre-dispatch (ADR 037 D3)"
            )
        hit = nodes_map.get(str(node_id))
        if hit is not None:
            if isinstance(hit, str) and hit.strip():
                return hit.strip()
            raise ModelClassResolutionError(
                f"model-class registry per-node class for pipeline '{pipeline_name}' node "
                f"'{node_id}' is malformed — run refused pre-dispatch (ADR 037 D3)"
            )
    default = entry.get("default")
    if default is None:
        return None
    if isinstance(default, str) and default.strip():
        return default.strip()
    raise ModelClassResolutionError(
        f"model-class registry pipelines entry for '{pipeline_name}' has a malformed 'default' "
        "— run refused pre-dispatch (ADR 037 D3)"
    )


def resolve_graph_model_classes(
    nodes: list[dict[str, Any]],
    *,
    pipeline_name: str,
    registry_path: str | None,
) -> None:
    """Resolve ``model_class`` for every sandbox_agent node and bake the result.

    Called at snapshot mint. Mutates each participating node in place:

    * ``_resolved_model`` — the concrete model id;
    * ``_model_origin`` — the precedence rung that supplied it (one of
      ``node_model_class`` / ``registry_pipeline_map`` /
      ``registry_pipeline_default``).

    Resolution engages only when there is something to resolve: a node declares
    ``model_class`` (registry required — fail closed if absent/unreadable), or
    a registry path is configured (its pipeline map / default rungs apply to
    every sandbox node — a configured-but-broken registry fails closed, D3).
    With neither, this is a no-op so existing literal-only pipelines keep
    working unchanged.

    Raises:
        ModelClassResolutionError: unknown class, missing/unreadable/corrupt
            registry, or a class with no ``model`` and no declared
            ``fallback`` — always naming the node where one is attributable.
    """
    sandbox_nodes = [n for n in nodes if isinstance(n, dict) and n.get("node_type") == "sandbox_agent"]
    if not sandbox_nodes:
        return
    opted_in = any(n.get("model_class") for n in sandbox_nodes)
    if not opted_in and not registry_path:
        # No first-class reference anywhere and no configured registry: the
        # registry rungs are unavailable, not failed — nothing participates.
        return

    registry = None
    try:
        registry = _read_registry(registry_path)
    except ModelClassResolutionError as exc:
        # D3: the refusal must name the NODE where one demanded the registry.
        # When no node opted in (a configured-but-broken registry consulted
        # only for the pipeline map/default rungs) the failure is attributed
        # to the pipeline instead — there is no node to name.
        opt_in = next((n for n in sandbox_nodes if n.get("model_class")), None)
        if opt_in is None:
            raise
        raise ModelClassResolutionError(
            f"sandbox_agent node '{opt_in.get('id')}' declares model_class "
            f"{opt_in.get('model_class')!r} but resolution failed: {exc.detail}"
        ) from exc
    excluded = {str(name) for name in (registry.get("excluded_pipelines") or [])}
    pipeline_excluded = pipeline_name in excluded

    for node in sandbox_nodes:
        declared = node.get("model_class")
        if declared is not None:
            if not isinstance(declared, str) or not declared.strip():
                raise ModelClassResolutionError(
                    f"sandbox_agent node '{node.get('id')}' declares a malformed model_class "
                    f"{declared!r} — run refused pre-dispatch (ADR 037 D3)"
                )
            class_name = declared.strip()
            origin = MODEL_ORIGIN_NODE_MODEL_CLASS
        else:
            if pipeline_excluded:
                # The registry's own "do not auto-classify" list gates only the
                # registry rungs — an explicit per-node model_class still wins.
                continue
            mapped = _pipeline_map_class(registry, pipeline_name, node.get("id"))
            origin = MODEL_ORIGIN_REGISTRY_PIPELINE_MAP
            if mapped is None:
                default = registry.get("pipelines_default")
                if not isinstance(default, str) or not default.strip():
                    continue
                mapped = default.strip()
                origin = MODEL_ORIGIN_REGISTRY_PIPELINE_DEFAULT
            class_name = mapped
        node["_resolved_model"] = _model_for_class(registry, class_name, node.get("id"))
        node["_model_origin"] = origin


def effective_model_template_value(node_def: dict[str, Any], run_context: dict[str, Any]) -> str | None:
    """The value for the top-level ``{{ model }}`` template variable at render.

    Precedence: the run's frozen ``_run_overrides.model`` (a literal model id
    set by the variant/A-B system, folded in pre-dispatch — run override wins)
    over the snapshot-baked ``_resolved_model``. Returns ``None`` when neither
    exists, in which case the render site decides (an opted-in node without a
    value fails closed; a node that never opted in simply has no variable).

    Reads the FULL ``run_context``, never the ``context_scope``-filtered view:
    the fold-in is a product mechanism, not a user template key. Never consults
    the registry — replay reads the SNAPSHOT's baked value, so a later registry
    edit cannot change what an already-minted run uses. Never mutates
    ``input``: ``{{ model }}`` is the dedicated resolution channel (caller
    input keeps its own meaning).
    """
    overrides = run_context.get("_run_overrides") if isinstance(run_context, dict) else None
    if isinstance(overrides, dict):
        raw = overrides.get("model")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    baked = node_def.get("_resolved_model") if isinstance(node_def, dict) else None
    if isinstance(baked, str) and baked.strip():
        return baked.strip()
    return None
