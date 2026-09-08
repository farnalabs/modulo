"""HTTP execution for ``modulo apply`` (FAR-681, slice 1).

Transport: sync httpx against MODULO_URL with the MODULO_API_KEY key header
(serves the API key so RLS/permissions scope every call to the org).

Applies schemas (+ versions) and model_backends per plan decisions. Per-entity
errors are captured into ``report["failed"]`` instead of aborting the run.
pipelines/triggers are slices 2/3 and intentionally absent.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from modulo.cli.apply.models import ApplyConfig, ModelBackendEntity, SchemaEntity
from modulo.cli.apply.plan import KIND_BACKEND, KIND_SCHEMA, build_plan, has_blockers

API_KEY_HEADER = "X-Api-Key"


def _entity_reports(config: ApplyConfig) -> dict[str, list[tuple[str, Any]]]:
    """kind -> [(name, entity-object)] preserving declaration order."""
    return {
        KIND_SCHEMA: [(e.name, e) for e in config.entities.schemas],
        KIND_BACKEND: [(e.name, e) for e in config.entities.model_backends],
    }


def resolve_secret_refs(
    config: ApplyConfig,
    environ: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Resolve ``${env:VAR}`` api_key refs client-side.

    Returns (resolved_api_key_by_backend_name, blocked) where blocked entries
    are (kind, name, reason) for backends whose env var is missing.
    secretref:// values pass through unresolved by design.
    """
    env: dict[str, str] = dict(environ if environ is not None else os.environ)
    resolved: dict[str, str] = {}
    blocked: list[tuple[str, str, str]] = []
    for entity in config.entities.model_backends:
        var = entity.env_ref_var()
        if var is None:
            resolved[entity.name] = entity.api_key
            continue
        value = env.get(var)
        if value is None:
            blocked.append((KIND_BACKEND, entity.name, f"unresolved env ref: ${{env:{var}}} is not set"))
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
        if client is None:
            self._client = httpx.Client(
                timeout=timeout,
                headers={API_KEY_HEADER: self._api_key},
            )
            self._owns_client = True
        else:
            self._client = client
            self._client.headers.update({API_KEY_HEADER: self._api_key})
            self._owns_client = False

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}/api/v1{path}"

    def _get(self, path: str) -> dict[str, Any]:
        response = self._client.get(self._url(path))
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(self._url(path), json=payload)
        if response.status_code >= 400:
            detail = _extract_detail(response)
            raise ApplyHttpError(f"POST {path} -> {response.status_code}: {detail}")
        return response.json()

    def _patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._client.patch(self._url(path), json=payload)
        if response.status_code >= 400:
            detail = _extract_detail(response)
            raise ApplyHttpError(f"PATCH {path} -> {response.status_code}: {detail}")
        return response.json()

    # ------------------------------------------------------------------
    # Current-state fetch
    # ------------------------------------------------------------------

    def fetch_current(
        self,
        config: ApplyConfig,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Fetch current org state for both kinds: kind -> {name: raw entity}.

        Schema versions are fetched for schemas whose desired entity declares
        versions (needed for hash comparison).
        """
        schemas_raw = self._get("/schemas?page=1&page_size=100")
        backends_raw = self._get("/model-backends?page=1&page_size=100")
        schemas = {item["name"]: item for item in schemas_raw.get("items", [])}
        backends = {item["name"]: item for item in backends_raw.get("items", [])}
        desired_with_versions = {e.name for e in config.entities.schemas if e.versions}
        for name in desired_with_versions:
            if name not in schemas:
                continue
            schema_id = schemas[name]["id"]
            versions_raw = self._get(f"/schemas/{schema_id}/versions")
            versions = [
                {
                    "version": v["version"],
                    "version_number": v["version_number"],
                    "definition_json": v["definition_json"],
                    "published": v.get("published", False),
                }
                for v in versions_raw.get("items", [])
                if v.get("version") != "latest"
            ]
            schemas[name]["versions"] = versions
        return {KIND_SCHEMA: schemas, KIND_BACKEND: backends}

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def apply_schemas(
        self,
        entities: dict[str, list[tuple[str, Any]]],
        current_entities: dict[str, dict[str, dict[str, Any]]],
        report: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Create/update schemas per the plan report, capturing failures."""
        for status in ("created", "updated"):
            for entry in list(report[status]):
                if entry["kind"] != KIND_SCHEMA:
                    continue
                entity = _find(entities[KIND_SCHEMA], entry["name"])
                assert isinstance(entity, SchemaEntity)
                try:
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
                        self._post(
                            f"/schemas/{schema_id}/versions",
                            {
                                "version": spec.version,
                                "version_number": spec.version_number,
                                "definition_json": spec.definition_json,
                                "published": spec.published,
                            },
                        )
                except (ApplyHttpError, httpx.HTTPError) as exc:
                    report[status].remove(entry)
                    report["failed"].append({"kind": KIND_SCHEMA, "name": entity.name, "error": str(exc)})

    def apply_backends(
        self,
        entities: dict[str, list[tuple[str, Any]]],
        current_entities: dict[str, dict[str, dict[str, Any]]],
        resolved_api_keys: dict[str, str],
        report: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Create/update model backends per the plan report, capturing failures."""
        for status in ("created", "updated"):
            for entry in list(report[status]):
                if entry["kind"] != KIND_BACKEND:
                    continue
                entity = _find(entities[KIND_BACKEND], entry["name"])
                assert isinstance(entity, ModelBackendEntity)
                try:
                    if status == "created":
                        self._post(
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
                    else:
                        current = current_entities[KIND_BACKEND][entity.name]
                        self._patch(
                            f"/model-backends/{current['id']}",
                            {
                                "display_name": entity.display_name,
                                "model_id": entity.model_id,
                                "default_params": entity.default_params,
                                "visibility": entity.visibility,
                                "tier": entity.tier,
                            },
                        )
                except (ApplyHttpError, httpx.HTTPError, KeyError) as exc:
                    report[status].remove(entry)
                    report["failed"].append(
                        {
                            "kind": KIND_BACKEND,
                            "name": entity.name,
                            "error": str(exc),
                        }
                    )

    def run(self, config: ApplyConfig, dry_run: bool) -> dict[str, Any]:
        """Resolve refs, plan, optionally execute, and return the report."""
        resolved_api_keys, blocked = resolve_secret_refs(config)
        entities = _entity_reports(config)
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
        }
        current_entities = self.fetch_current(config) if any(desired.values()) else {KIND_SCHEMA: {}, KIND_BACKEND: {}}
        report = build_plan(desired, current_entities, blocked)
        report["dry_run"] = dry_run
        report["failed"] = []
        if dry_run:
            return report
        self.apply_backends(entities, current_entities, resolved_api_keys, report)
        self.apply_schemas(entities, current_entities, report)
        return report


class ApplyHttpError(RuntimeError):
    """Raised for non-2xx API responses during apply execution."""


def _extract_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"status {response.status_code}"
    if isinstance(body, dict):
        return str(body.get("detail", body))
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
    "API_KEY_HEADER",
    "ApplyExecutor",
    "ApplyHttpError",
    "has_blockers",
    "resolve_secret_refs",
]
