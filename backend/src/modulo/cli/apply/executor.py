"""HTTP execution for ``modulo apply`` (FAR-681, slices 1+2).

Transport: sync httpx against MODULO_URL with ``Authorization: Bearer
<MODULO_API_KEY>`` (the server authenticates bearer tokens as either a user
JWT or an ``mk_`` org API key; the API key scopes RLS/permissions to the org).

Applies schemas (+ versions), model_backends, pipelines (agent name-refs
resolved via the /agents map; graphs normalised through the real API models)
and triggers per plan decisions, in that dependency order. Per-entity errors
are captured into ``report["failed"]`` instead of aborting the run.
"""

from __future__ import annotations

import logging
import os
from typing import Any, cast

import httpx

from modulo.cli.apply.models import ApplyConfig, ModelBackendEntity, SchemaEntity
from modulo.cli.apply.plan import (
    KIND_BACKEND,
    KIND_PIPELINE,
    KIND_SCHEMA,
    KIND_TRIGGER,
    build_plan,
    has_blockers,
)

_log = logging.getLogger(__name__)

# The server (auth/dependencies.py HTTPBearer) reads ONLY the Authorization
# header — X-Api-Key is never inspected. Bearer + mk_ key is the documented
# CI/CD credential.
AUTH_HEADER = "Authorization"
PAGE_SIZE = 100
_SECRETREF_BLOCK_REASON = "secretref resolution not supported yet (server-side resolution lands in a later slice)"
_CONFLICT_HINT = "name already exists; rerun to apply as update"


def _entity_reports(config: ApplyConfig) -> dict[str, list[tuple[str, Any]]]:
    """kind -> [(name, entity-object)] preserving declaration order.

    Triggers are keyed by their (pipeline, name) composite display key.
    """
    return {
        KIND_SCHEMA: [(e.name, e) for e in config.entities.schemas],
        KIND_BACKEND: [(e.name, e) for e in config.entities.model_backends],
        KIND_PIPELINE: [(e.name, e) for e in config.entities.pipelines],
        KIND_TRIGGER: [(e.display_key(), e) for e in config.entities.triggers],
    }


def resolve_secret_refs(
    config: ApplyConfig,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Resolve ``${env:VAR}`` api_key refs client-side.

    Returns (resolved_api_key_by_backend_name, blocked) where blocked entries
    are (kind, name, reason) for backends whose env var is missing/empty, and
    for secretref:// refs (no server-side resolution exists in this slice, so
    they would be stored as non-functional literals).
    """
    env: dict[str, str] = dict(environ if environ is not None else os.environ)
    resolved: dict[str, str] = {}
    blocked: list[tuple[str, str, str]] = []
    for entity in config.entities.model_backends:
        var = entity.env_ref_var()
        if var is None:
            if entity.api_key.startswith("secretref://"):
                blocked.append((KIND_BACKEND, entity.name, _SECRETREF_BLOCK_REASON))
            continue
        value = env.get(var)
        if value is None:
            blocked.append((KIND_BACKEND, entity.name, f"unresolved env ref: ${{env:{var}}} is not set"))
        elif not value.strip():
            blocked.append(
                (KIND_BACKEND, entity.name, f"unresolved env ref: ${{env:{var}}} resolves to an empty value")
            )
        else:
            resolved[entity.name] = value
    return resolved, blocked


class ApplyExecutor:
    """Executes an ApplyConfig against the live HTTP API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._auth_headers = {AUTH_HEADER: f"Bearer {api_key}"}
        if client is None:
            # CLI egress residual (no sync pinned-egress factory exists in
            # core/ssrf.py - it only builds pinned ASYNC pools, and this click
            # executor is sync). The base URL is the operator-configured
            # MODULO_URL, the same deployment the operator's own credentials
            # already target; excluded from the semgrep no-raw-httpx-client
            # gate as documented residual, like the operator-config support
            # modules rather than as accepted raw perimeter egress.
            self._client = httpx.Client(
                timeout=timeout,
                follow_redirects=False,
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}/api/v1{path}"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.get(self._url(path), params=params, headers=self._auth_headers)
        if not 200 <= response.status_code < 300:
            detail = _extract_detail(response)
            raise ApplyHttpError(
                f"GET {path} -> {response.status_code}: {detail}",
                status_code=response.status_code,
                method="GET",
                path=path,
            )
        try:
            return cast("dict[str, Any]", response.json())
        except ValueError as exc:
            raise ApplyHttpError(
                f"GET {path} returned invalid JSON: {exc}",
                status_code=response.status_code,
                method="GET",
                path=path,
            ) from None

    def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._client.post(self._url(path), json=payload, headers=self._auth_headers)
        if not 200 <= response.status_code < 300:
            detail = _extract_detail(response)
            raise ApplyHttpError(
                f"POST {path} -> {response.status_code}: {detail}",
                status_code=response.status_code,
                method="POST",
                path=path,
            )
        try:
            return cast("dict[str, Any]", response.json())
        except ValueError as exc:
            raise ApplyHttpError(
                f"POST {path} returned invalid JSON: {exc}",
                status_code=response.status_code,
                method="POST",
                path=path,
            ) from None

    def _patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.patch(self._url(path), json=payload, headers=self._auth_headers)
        if not 200 <= response.status_code < 300:
            detail = _extract_detail(response)
            raise ApplyHttpError(
                f"PATCH {path} -> {response.status_code}: {detail}",
                status_code=response.status_code,
                method="PATCH",
                path=path,
            )
        try:
            return cast("dict[str, Any]", response.json())
        except ValueError as exc:
            raise ApplyHttpError(
                f"PATCH {path} returned invalid JSON: {exc}",
                status_code=response.status_code,
                method="PATCH",
                path=path,
            ) from None

    def _put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.put(self._url(path), json=payload, headers=self._auth_headers)
        if not 200 <= response.status_code < 300:
            detail = _extract_detail(response)
            raise ApplyHttpError(
                f"PUT {path} -> {response.status_code}: {detail}",
                status_code=response.status_code,
                method="PUT",
                path=path,
            )
        try:
            return cast("dict[str, Any]", response.json())
        except ValueError as exc:
            raise ApplyHttpError(
                f"PUT {path} returned invalid JSON: {exc}",
                status_code=response.status_code,
                method="PUT",
                path=path,
            ) from None

    # ------------------------------------------------------------------
    # Current-state fetch
    # ------------------------------------------------------------------

    def _get_paginated(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Collect every item across all pages (migrate_org paginate pattern).

        Loops until collected >= total or a page comes back empty, so orgs
        larger than one page are fully fetched instead of silently truncated.
        """
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            page_params: dict[str, Any] = {"page": page, "page_size": PAGE_SIZE, **(params or {})}
            payload = self._get(path, params=page_params)
            batch = payload.get("items", [])
            items.extend(batch)
            total = payload.get("total")
            if not batch or (total is not None and len(items) >= total):
                break
            page += 1
        return items

    def fetch_current(
        self,
        config: ApplyConfig,
    ) -> dict[str, Any]:
        """Fetch current org state: kind -> {name: raw entity} (+ lookups).

        Schema versions are fetched for schemas whose desired entity declares
        versions (needed for hash comparison). Model backends are fetched with
        ``include_in_dev=true`` so in_dev-tier entities are not missed as false
        "created" decisions; a 403 (operator-tier key cannot reveal in_dev)
        falls back to the default exclusion.

        Pipelines/triggers/agents lists are fetched only when the config
        declares those kinds; /agents LAZILY — only when some declared
        pipeline node actually references an agent (every avoided list call
        is one less failure surface for an API-key principal). Pipeline
        graphs are fetched (and normalised to the hash shape via the real
        API models) for desired pipelines that exist in the org; a stored
        graph the models reject is contained — the pipeline is demoted to
        blocked (``unreadable_graph_pipelines``) and the rest still plans.

        Triggers are keyed by ``pipeline/name`` (pre-name rows and triggers
        whose pipeline is invisible are unmanaged by apply); the ``agents``
        entry maps agent name -> id for node ref resolution. Duplicate-name
        findings are reported as ``ambiguous_pipeline_names`` /
        ``ambiguous_agent_names`` (name -> match count; ambiguous names are
        EXCLUDED from the maps — name-based upsert cannot pick a row) and
        duplicate live trigger composite keys as ``duplicate_trigger_keys``.
        """
        schemas_list = self._get_paginated("/schemas")
        try:
            backends_list = self._get_paginated("/model-backends", params={"include_in_dev": "true"})
        except ApplyHttpError as exc:
            if exc.status_code != 403:
                raise
            _log.warning(
                "apply.fetch_backends_in_dev_forbidden: falling back to default listing (missing in_dev entities)"
            )
            backends_list = self._get_paginated("/model-backends")
        schemas = {item["name"]: item for item in schemas_list}
        backends = {item["name"]: item for item in backends_list}
        desired_with_versions = {e.name for e in config.entities.schemas if e.versions}
        for name in desired_with_versions:
            if name not in schemas:
                continue
            schema_id = schemas[name]["id"]
            versions_list = self._get_paginated(f"/schemas/{schema_id}/versions")
            versions = [
                {
                    "version": v["version"],
                    "version_number": v["version_number"],
                    "definition_json": v["definition_json"],
                    "published": v.get("published", False),
                }
                for v in versions_list
                if v.get("version") != "latest"
            ]
            schemas[name]["versions"] = versions

        from modulo.cli.apply import pipeline_apply

        wants_pipelines = bool(config.entities.pipelines)
        wants_triggers = bool(config.entities.triggers)
        pipelines_list: list[dict[str, Any]] = []
        agents_list: list[dict[str, Any]] = []
        triggers_list: list[dict[str, Any]] = []
        if wants_pipelines or wants_triggers:
            pipelines_list = self._get_paginated("/pipelines")
        # /agents is fetched LAZILY: only when a declared pipeline node
        # actually references an agent (FAR-681 QA — a graph-less or
        # agent-free config never needs the agents list).
        needs_agents = any(
            entity.graph is not None and any(node.agent is not None for node in entity.graph.nodes)
            for entity in config.entities.pipelines
        )
        if needs_agents:
            agents_list = self._get_paginated("/agents")
        if wants_triggers:
            triggers_list = self._get_paginated("/triggers")

        ambiguous_agent_names = _duplicate_name_counts(agents_list)
        agents = {
            item["name"]: item["id"]
            for item in agents_list
            if item.get("name") is not None and item["name"] not in ambiguous_agent_names
        }
        ambiguous_pipeline_names = _duplicate_name_counts(pipelines_list)
        pipelines = {
            item["name"]: item
            for item in pipelines_list
            if item.get("name") is not None and item["name"] not in ambiguous_pipeline_names
        }
        unreadable_graph_pipelines: dict[str, str] = {}
        if wants_pipelines:
            # Only pipelines whose config DECLARES a graph need the fetched
            # graph (a graph-less config does not manage the graph at all).
            for name in {e.name for e in config.entities.pipelines if e.graph is not None} & pipelines.keys():
                try:
                    graph_payload = self._get(f"/pipelines/{pipelines[name]['id']}/graph")
                    pipelines[name]["graph"] = pipeline_apply.normalize_current_graph(graph_payload)
                except (ApplyHttpError, ValueError, KeyError, TypeError) as exc:
                    # A stored graph the models reject (legacy data, version
                    # skew) must not abort the whole run: demote this pipeline
                    # to blocked ("current graph unreadable") and keep
                    # planning the rest of the config.
                    reason = str(exc) or exc.__class__.__name__
                    _log.warning("apply.fetch_graph_unreadable: %s: %s", name, reason)
                    unreadable_graph_pipelines[name] = reason
        # Trigger identity map: pipeline ids -> names. Pipeline ids whose name
        # is ambiguous are EXCLUDED (their composite key would be unreliable).
        pipeline_names_by_id = {
            str(item["id"]): item["name"]
            for item in pipelines_list
            if item.get("id") is not None
            and item.get("name") is not None
            and item["name"] not in ambiguous_pipeline_names
        }
        triggers: dict[str, dict[str, Any]] = {}
        trigger_key_counts: dict[str, int] = {}
        for item in triggers_list:
            trigger_name = item.get("name")
            pipeline_name = pipeline_names_by_id.get(str(item.get("pipeline_id")))
            if not trigger_name or pipeline_name is None:
                continue
            key = f"{pipeline_name}/{trigger_name}"
            trigger_key_counts[key] = trigger_key_counts.get(key, 0) + 1
        duplicate_trigger_keys = {key for key, count in trigger_key_counts.items() if count > 1}
        for item in triggers_list:
            trigger_name = item.get("name")
            pipeline_name = pipeline_names_by_id.get(str(item.get("pipeline_id")))
            if not trigger_name or pipeline_name is None:
                continue
            key = f"{pipeline_name}/{trigger_name}"
            if key in duplicate_trigger_keys:
                continue
            triggers[key] = item
        return {
            KIND_SCHEMA: schemas,
            KIND_BACKEND: backends,
            KIND_PIPELINE: pipelines,
            KIND_TRIGGER: triggers,
            "agents": agents,
            "ambiguous_agent_names": ambiguous_agent_names,
            "ambiguous_pipeline_names": ambiguous_pipeline_names,
            "duplicate_trigger_keys": duplicate_trigger_keys,
            "unreadable_graph_pipelines": unreadable_graph_pipelines,
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _verify_backend_health(self, backend_id: str, name: str) -> str | None:
        """Re-run the server-side health check; return a failure message or None.

        The create/update API runs a best-effort health check on save but never
        fails the request, so apply must poll the health-check endpoint to
        surface an unhealthy backend (stored credentials that do not work)
        instead of reporting silent success.
        """
        response = self._post(f"/model-backends/{backend_id}/health-check")
        if response.get("status") == "unhealthy":
            detail = response.get("detail") or "health check reported unhealthy without detail"
            return f"model backend {name!r} failed health check: {detail}"
        return None

    def apply_schemas(
        self,
        entities: dict[str, list[tuple[str, Any]]],
        current_entities: dict[str, dict[str, dict[str, Any]]],
        report: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Create/update schemas per the plan report, capturing failures.

        Containment is per-entity: one unexpected response shape or HTTP error
        moves exactly that entity (or, for a version POST, that one version)
        to ``failed``; later entities still apply.
        """
        for status in ("created", "updated"):
            for entry in list(report[status]):
                if entry["kind"] != KIND_SCHEMA:
                    continue
                entity = None
                try:
                    entity = _find(entities[KIND_SCHEMA], entry["name"])
                    assert isinstance(entity, SchemaEntity)
                    if status == "created":
                        response = self._post(
                            "/schemas",
                            {
                                "name": entity.name,
                                "description": entity.description,
                                "abstract_name": entity.abstract_name,
                            },
                        )
                        schema_id = response["id"]
                    else:
                        current = current_entities[KIND_SCHEMA][entity.name]
                        schema_id = current["id"]
                        payload: dict[str, Any] = {}
                        if entity.description != current.get("description"):
                            payload["description"] = entity.description
                        if entity.abstract_name != current.get("abstract_name"):
                            payload["abstract_name"] = entity.abstract_name
                        if payload:
                            self._patch(f"/schemas/{schema_id}", payload)
                    existing_versions = (
                        {v["version"] for v in current_entities[KIND_SCHEMA][entity.name].get("versions") or []}
                        if status == "updated"
                        else set()
                    )
                    for spec in entity.versions:
                        if spec.version in existing_versions:
                            continue
                        try:
                            self._post(
                                f"/schemas/{schema_id}/versions",
                                {
                                    "version": spec.version,
                                    "version_number": spec.version_number,
                                    "definition_json": spec.definition_json,
                                    "published": spec.published,
                                },
                            )
                        except (ApplyHttpError, httpx.HTTPError, KeyError, ValueError) as exc:
                            # The schema itself was created/updated; only the
                            # version failed. Keep the entity entry (truthful)
                            # and add a version-scoped failed entry.
                            message = _failure_message(exc)
                            _log.warning("apply schema version failed: %s/%s: %s", entity.name, spec.version, message)
                            report["failed"].append(
                                {
                                    "kind": "schema_version",
                                    "name": f"{entity.name}/{spec.version}",
                                    "error": message,
                                }
                            )
                except (ApplyHttpError, httpx.HTTPError, KeyError, ValueError) as exc:
                    message = _failure_message(exc)
                    _log.warning("apply schema failed: %s: %s", entity.name if entity else entry["name"], message)
                    report[status].remove(entry)
                    report["failed"].append({"kind": KIND_SCHEMA, "name": entry["name"], "error": message})

    def apply_backends(
        self,
        entities: dict[str, list[tuple[str, Any]]],
        current_entities: dict[str, dict[str, dict[str, Any]]],
        resolved_api_keys: dict[str, str],
        report: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Create/update model backends per the plan report, capturing failures.

        After each write the server-side health-check endpoint is re-run; an
        unhealthy result moves the entry to ``failed`` (driving exit 1) so a
        stored-but-broken credential is never reported as plain success.
        """
        for status in ("created", "updated"):
            for entry in list(report[status]):
                if entry["kind"] != KIND_BACKEND:
                    continue
                entity = None
                try:
                    entity = _find(entities[KIND_BACKEND], entry["name"])
                    assert isinstance(entity, ModelBackendEntity)
                    if status == "created":
                        response = self._post(
                            "/model-backends",
                            {
                                "name": entity.name,
                                "display_name": entity.display_name,
                                "provider": entity.provider,
                                "model_id": entity.model_id,
                                "api_key": resolved_api_keys[entity.name],
                                "default_params": entity.default_params,
                                "visibility": entity.visibility,
                                "tier": entity.tier,
                            },
                        )
                        backend_id = response["id"]
                    else:
                        current = current_entities[KIND_BACKEND][entity.name]
                        backend_id = current["id"]
                        self._patch(
                            f"/model-backends/{backend_id}",
                            {
                                "display_name": entity.display_name,
                                "model_id": entity.model_id,
                                "default_params": entity.default_params,
                                "visibility": entity.visibility,
                                "tier": entity.tier,
                            },
                        )
                    health_error = self._verify_backend_health(backend_id, entity.name)
                except (ApplyHttpError, httpx.HTTPError, KeyError, ValueError) as exc:
                    message = _failure_message(exc)
                    displayed_name = entity.name if entity else entry["name"]
                    _log.warning("apply model backend failed: %s: %s", displayed_name, message)
                    report[status].remove(entry)
                    report["failed"].append({"kind": KIND_BACKEND, "name": entry["name"], "error": message})
                else:
                    if health_error:
                        _log.warning("apply model backend unhealthy: %s: %s", entity.name, health_error)
                        report[status].remove(entry)
                        report["failed"].append(
                            {
                                "kind": KIND_BACKEND,
                                "name": entry["name"],
                                "error": f"{status} but failed health check - {health_error}",
                            }
                        )

    def run(self, config: ApplyConfig, dry_run: bool, refresh_secrets: bool = False) -> dict[str, Any]:
        """Resolve refs, plan, optionally execute, and return the report.

        Phase order: schemas -> model_backends -> pipelines -> triggers
        (forward-dependency order; a trigger's pipeline is applied before the
        trigger). Pipeline/trigger resolution failures enter ``blocked`` with
        per-entity containment identical to the slice-1 kinds. With
        ``refresh_secrets`` set, triggers whose raw config declares
        secret-shaped entries always re-send their config (secret-only
        rotation is invisible to the drift hash otherwise).
        """
        from modulo.cli.apply import pipeline_apply, trigger_apply

        resolved_api_keys, blocked = resolve_secret_refs(config)
        entities = _entity_reports(config)
        current_entities: dict[str, Any] = self.fetch_current(config) if any(entities.values()) else _empty_current()
        resolved_trigger_configs, trigger_blocked = trigger_apply.resolve_trigger_config_refs(entities)
        blocked = [*blocked, *trigger_blocked]
        # Current-state findings (duplicate live trigger names, unreadable
        # stored graphs) block the affected entities BEFORE planning. Duplicates
        # only matter for entities this config declares.
        declared_trigger_keys = {e.display_key() for e in config.entities.triggers}
        for composite_key in current_entities.get("duplicate_trigger_keys") or set():
            if composite_key in declared_trigger_keys:
                blocked.append((KIND_TRIGGER, composite_key, "duplicate live trigger name - resolve manually"))
        for name, reason in (current_entities.get("unreadable_graph_pipelines") or {}).items():
            blocked.append((KIND_PIPELINE, name, f"current graph unreadable: {reason}"))
        blocked_keys = {(kind, name) for kind, name, _reason in blocked}
        desired: dict[str, list[tuple[str, dict[str, Any]]]] = {
            KIND_SCHEMA: [
                (name, e.managed_view()) for name, e in entities[KIND_SCHEMA] if (KIND_SCHEMA, name) not in blocked_keys
            ],
            KIND_BACKEND: [
                (name, e.managed_view())
                for name, e in entities[KIND_BACKEND]
                if (KIND_BACKEND, name) not in blocked_keys
            ],
            KIND_PIPELINE: [],
            KIND_TRIGGER: [],
        }
        desired, blocked = pipeline_apply.build_desired_views(
            config.entities, current_entities, desired, blocked, blocked_keys
        )
        # The pipeline build phase appended its own blocked entries AFTER
        # blocked_keys was computed — recompute so trigger planning sees the
        # upstream pipeline blocks (otherwise a trigger would be planned
        # against a pipeline that will never apply: a dry-run lie).
        blocked_keys.update((kind, name) for kind, name, _reason in blocked)
        pipeline_block_reasons = {name: reason for kind, name, reason in blocked if kind == KIND_PIPELINE}
        desired, blocked = trigger_apply.build_desired_views(
            config.entities,
            current_entities,
            desired,
            blocked,
            blocked_keys,
            resolved_configs=resolved_trigger_configs,
            pipeline_block_reasons=pipeline_block_reasons,
            refresh_secrets=refresh_secrets,
        )
        report: dict[str, Any] = build_plan(desired, current_entities, blocked)
        report["dry_run"] = dry_run
        report["failed"] = []
        if dry_run:
            return report
        self.apply_schemas(entities, current_entities, report)
        self.apply_backends(entities, current_entities, resolved_api_keys, report)
        pipeline_ids = pipeline_apply.apply_pipelines(self, entities, current_entities, report)
        trigger_apply.apply_triggers(self, entities, current_entities, report, pipeline_ids, resolved_trigger_configs)
        return report


def _empty_current() -> dict[str, Any]:
    """The no-entity current-state shape (nothing declared -> nothing fetched)."""
    return {
        KIND_SCHEMA: {},
        KIND_BACKEND: {},
        KIND_PIPELINE: {},
        KIND_TRIGGER: {},
        "agents": {},
        "ambiguous_agent_names": {},
        "ambiguous_pipeline_names": {},
        "duplicate_trigger_keys": set(),
        "unreadable_graph_pipelines": {},
    }


def _duplicate_name_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    """Names appearing MORE THAN ONCE in a fetched list, with match counts.

    A duplicate name makes name-based upsert ambiguous (last-wins would
    silently pick a row); callers block the affected entities instead.
    """
    counts: dict[str, int] = {}
    for item in items:
        name = item.get("name")
        if name is not None:
            counts[name] = counts.get(name, 0) + 1
    return {name: count for name, count in counts.items() if count > 1}


class ApplyHttpError(RuntimeError):
    """Raised for non-2xx/3xx API responses (or unparseable bodies) during apply."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str = "",
        path: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.path = path


def _failure_message(exc: Exception) -> str:
    """Human-readable per-entity failure message with a 409 remediation hint."""
    message = str(exc)
    if isinstance(exc, ApplyHttpError) and exc.status_code == 409:
        message += f" ({_CONFLICT_HINT})"
    return message


def _extract_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"status {response.status_code}"
    if isinstance(body, dict):
        # get-with-fallback is forbidden here (semgrep output-get-fallback-parent):
        # a missing detail key must not silently fall back to the whole payload.
        if "detail" in body:
            detail = body["detail"]
            if detail is not None:
                return str(detail)
        return f"status {response.status_code}: {body}"
    return f"status {response.status_code}: {body}"


def _find(
    pairs: list[tuple[str, Any]],
    name: str,
) -> Any:
    for candidate_name, entity in pairs:
        if candidate_name == name:
            return entity
    msg = f"entity {name} missing from desired set"
    raise KeyError(msg)


__all__ = [
    "AUTH_HEADER",
    "ApplyExecutor",
    "ApplyHttpError",
    "has_blockers",
    "resolve_secret_refs",
]
