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

import json
import logging
import os
from typing import Any, NamedTuple, cast

import httpx

from modulo.cli.apply.models import (
    SECRET_REF_SCHEME,
    ApplyConfig,
    ModelBackendEntity,
    SchemaEntity,
)
from modulo.cli.apply.plan import (
    KIND_BACKEND,
    KIND_PIPELINE,
    KIND_SCHEMA,
    KIND_TRIGGER,
    build_plan,
    has_blockers,
)
from modulo.core.model_backend_endpoints import BaseUrlValidationError, validate_base_url

_log = logging.getLogger(__name__)

# The server (auth/dependencies.py HTTPBearer) reads ONLY the Authorization
# header — X-Api-Key is never inspected. Bearer + mk_ key is the documented
# CI/CD credential.
AUTH_HEADER = "Authorization"
PAGE_SIZE = 100
_SECRETREF_BLOCK_REASON = "secretref resolution not supported yet (server-side resolution lands in a later slice)"
_CONFLICT_HINT = "name already exists; rerun to apply as update"

# FAR-2105: the FAR-2046 credential-binding rule, surfaced at PLAN time.
# Changing a base_url must be paired with the LITERAL key itself: the server
# re-authenticates a new destination with the credential (so the credential
# owner, not an operator holding only a vault reference, authorises the move).
# A ``credential_ref`` / ``secretref://`` token would only reach a 422 halfway
# through an apply, leaving a partial run — so the entity is blocked up front.
_ENDPOINT_CHANGE_CREDENTIAL_REASON = (
    "changing base_url requires a literal api_key: the server re-authenticates a new "
    "destination with the key itself, so a credential_ref / secretref:// vault "
    "reference cannot change an endpoint (declare api_key: ${env:VAR} and put the key "
    "in the environment)"
)
_ENDPOINT_CREATE_CREDENTIAL_REASON = (
    "a custom base_url requires a literal api_key: a credential_ref / secretref:// "
    "vault reference is not accepted for a new destination"
)
_ENDPOINT_UNSUPPORTED_REASON = (
    "cannot manage base_url: the API did not report an endpoint for this backend - "
    "is the server running a version that predates custom model-backend endpoints?"
)

# Repeated API paths (S1192).
_PATH_MODEL_BACKENDS = "/model-backends"
_PATH_SCHEMAS = "/schemas"
# FAR-1109: node policy-gate eval_id references resolve against the org's
# evals (list route filters soft-deleted rows).
_PATH_EVALS = "/evals"

# The MCP StreamableHTTP endpoint (stateless JSON-RPC) — the org-vault key
# listing surface (FAR-1216). The server exposes no REST route that reads the
# vault; ``list_secrets`` is the shipped read-only key-listing tool, so
# plan-time credential_ref validation uses it rather than inventing a parallel
# surface.
_MCP_TOOLS_CALL_PATH = "/mcp/"
_MCP_LIST_SECRETS_LIMIT = 1000
_MCP_ACCEPT = "application/json, text/event-stream"


class VaultListingError(RuntimeError):
    """The org vault key listing could not be fetched.

    Raised (never swallowed) so a caller validating a ``credential_ref`` fails
    CLOSED with a clear reason rather than silently passing a key it could not
    confirm.
    """


def _is_vault_credential(entity: ModelBackendEntity) -> bool:
    """True when the declared credential is a server-resolved vault reference.

    Both declarative spellings normalise to the same wire token
    (``secretref://<key>``), so both are non-literal for the FAR-2046
    endpoint-binding rule.
    """
    if entity.credential_ref is not None:
        return True
    return entity.api_key is not None and entity.api_key.startswith(SECRET_REF_SCHEME)


def _declared_endpoint_change(entity: ModelBackendEntity, current: dict[str, Any]) -> bool:
    """True when the config's declared endpoint differs from the live one.

    Only a DECLARED ``base_url`` is managed; an omitted key leaves the live
    endpoint untouched (never a change, never a write).
    """
    return entity.manages_base_url and entity.base_url != current.get("base_url")


def _add_declared_endpoint(payload: dict[str, Any], entity: ModelBackendEntity) -> None:
    """Copy the declared FAR-2046 endpoint fields onto a write payload.

    Only called when ``base_url`` is managed (declared). An explicit null is
    sent as null — the server's own "clear the stored endpoint" signal — while
    ``allow_insecure_http`` is sent only for a non-null endpoint (it gates URL
    validation and is meaningless alongside a clear). The raw declared flag is
    sent, not the scheme-derived hash form: the server needs the operator's
    actual opt-in to accept a plain-``http`` destination.
    """
    if not entity.manages_base_url:
        return
    payload["base_url"] = entity.base_url
    if entity.base_url is not None and entity.allow_insecure_http is not None:
        payload["allow_insecure_http"] = entity.allow_insecure_http


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
    """Resolve ``${env:VAR}`` api_key refs client-side; forward vault refs.

    Returns (api_key_by_backend_name, blocked) where blocked entries are
    (kind, name, reason) for backends whose env var is missing/empty.

    A ``secretref://<key>`` api_key — or a ``credential_ref: <key>``, which is
    normalised to the same token (FAR-1216) — is forwarded to the server
    UNCHANGED (the ``secretref://...`` string is sent as the request's
    ``api_key``): the SERVER resolves it against the org vault at write time
    under the caller's org context (FAR-1640) and fails the entity with a
    typed, key-naming error if it is missing/foreign. The value never crosses
    the client boundary, so the CLI cannot and does not resolve it locally.
    """
    env: dict[str, str] = dict(environ if environ is not None else os.environ)
    resolved: dict[str, str] = {}
    blocked: list[tuple[str, str, str]] = []
    for entity in config.entities.model_backends:
        credential_reference = entity.credential_reference()
        if credential_reference is not None:
            # ``credential_ref: <key>`` — forward the normalised token; the
            # server resolves the vault reference (FAR-1640).
            resolved[entity.name] = credential_reference
            continue
        var = entity.env_ref_var()
        if var is None:
            # Literal or ``secretref://<key>`` — pass through; the server
            # resolves the vault reference (FAR-1640).
            assert entity.api_key is not None  # enforced by the model validator
            resolved[entity.name] = entity.api_key
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


def _parse_vault_key_listing(envelope: dict[str, Any]) -> set[str]:
    """Extract the org vault keys from a ``list_secrets`` MCP JSON-RPC response.

    Raises :class:`VaultListingError` for any error/partial response so a
    caller never mistakes "could not list" for "key absent" (which would block
    a valid config) or "key present" (a silent pass).
    """
    error = envelope.get("error")
    if error is not None:
        detail = error.get("message") if isinstance(error, dict) else error
        raise VaultListingError(f"list_secrets returned an error: {detail!r}")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise VaultListingError("list_secrets response carried no result")
    if result.get("isError"):
        raise VaultListingError("list_secrets reported a tool error")
    content = result.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], dict):
        raise VaultListingError("list_secrets response carried no content")
    text = content[0].get("text")
    if not isinstance(text, str):
        raise VaultListingError("list_secrets content was not text")
    try:
        data = json.loads(text)
    except ValueError:
        raise VaultListingError("list_secrets content was not JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("secrets"), list):
        raise VaultListingError("list_secrets payload carried no secrets list")
    keys = {item["key"] for item in data["secrets"] if isinstance(item, dict) and isinstance(item.get("key"), str)}
    total = data.get("total")
    if isinstance(total, int) and total > len(data["secrets"]):
        # The page limit truncated the listing: a missing key cannot be
        # distinguished from a key beyond the page, so refuse to validate.
        raise VaultListingError(f"vault holds {total} keys but only {len(data['secrets'])} were listed")
    return keys


class _PipelineBundle(NamedTuple):
    """Pipeline-family slices of ``fetch_current`` (S3776 split).

    Returned by :meth:`ApplyExecutor._fetch_pipeline_bundle`; the
    orchestrator assembles these into the same ``fetch_current`` result
    dict the monolith used to build inline.
    """

    agents: dict[str, str]
    ambiguous_agent_names: dict[str, int]
    pipelines: dict[str, dict[str, Any]]
    ambiguous_pipeline_names: dict[str, int]
    unreadable_graph_pipelines: dict[str, str]
    users: dict[str, str]
    users_error: str | None
    evals: dict[str, dict[str, Any]]
    evals_error: str | None


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
    # Org-vault key listing (FAR-1216 plan-time credential_ref validation)
    # ------------------------------------------------------------------

    def list_vault_keys(self) -> set[str]:
        """Return the org vault secret keys via the MCP ``list_secrets`` tool.

        The server ships no REST route that reads the vault; ``list_secrets``
        (keys only, never values) is the read-only surface. Raises
        :class:`VaultListingError` on any failure so validation fails CLOSED.
        """
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "list_secrets",
                "arguments": {"limit": _MCP_LIST_SECRETS_LIMIT},
            },
        }
        try:
            response = self._client.post(
                f"{self._base_url}{_MCP_TOOLS_CALL_PATH}",
                json=request,
                headers={**self._auth_headers, "Accept": _MCP_ACCEPT},
            )
        except httpx.HTTPError as exc:
            raise VaultListingError(f"vault listing request failed ({type(exc).__name__})") from None
        if not 200 <= response.status_code < 300:
            raise VaultListingError(f"vault listing returned status {response.status_code}")
        try:
            envelope = response.json()
        except ValueError:
            raise VaultListingError("vault listing returned invalid JSON") from None
        if not isinstance(envelope, dict):
            raise VaultListingError("vault listing returned a non-object response")
        return _parse_vault_key_listing(envelope)

    def _block_missing_credential_refs(self, config: ApplyConfig) -> list[tuple[str, str, str]]:
        """Block model backends whose ``credential_ref`` is absent from the vault.

        Runs at PLAN time (before any write). Only entered when the config
        actually declares a ``credential_ref`` — configs using a literal /
        ``${env:}`` / ``secretref://`` api_key never incur the listing call.
        A key that cannot be verified (listing unavailable) blocks too: a plan
        that cannot confirm the key must not silently pass it.
        """
        declared = [(e.name, e.credential_ref) for e in config.entities.model_backends if e.credential_ref is not None]
        if not declared:
            return []
        try:
            vault_keys = self.list_vault_keys()
        except VaultListingError as exc:
            _log.warning("apply.vault_listing_unavailable: %s", exc)
            return [(KIND_BACKEND, name, f"cannot verify credential_ref {ref!r}: {exc}") for name, ref in declared]
        return [
            (KIND_BACKEND, name, f"credential_ref {ref!r} not found in the org vault")
            for name, ref in declared
            if ref not in vault_keys
        ]

    def _block_unmanageable_endpoints(
        self,
        config: ApplyConfig,
        current_entities: dict[str, Any],
        already_blocked: set[str],
    ) -> list[tuple[str, str, str]]:
        """Block endpoint declarations that cannot succeed, BEFORE any write.

        Two plan-time guarantees for the FAR-2046 endpoint (FAR-2105):

        1. **Shared validation.** A declared ``base_url`` is validated with
           ``core.model_backend_endpoints.validate_base_url`` — the SAME rules
           the REST route and the FAR-2067 backfill apply — so a metadata host,
           a non-canonical IP literal or plain ``http`` without the opt-in
           fails at plan with the validator's own message instead of a 422
           halfway through an apply. The provider capability map is the
           SERVER's authority (``core.model_backend_hub``): the CLI does not
           restate it, so an endpoint on an incapable provider still fails at
           the server with its typed error.
        2. **Credential binding.** A custom destination must be paired with the
           literal key (FAR-2046): on create a vault reference is refused, and
           on update any CHANGE of destination requires the key itself. A
           config that can only supply ``credential_ref`` / ``secretref://``
           therefore cannot create or move an endpoint — blocked here with the
           cause named.
        """
        blocked: list[tuple[str, str, str]] = []
        current_backends = current_entities.get(KIND_BACKEND) or {}
        for entity in config.entities.model_backends:
            if entity.name in already_blocked or not entity.manages_base_url:
                continue
            declared = entity.base_url
            if declared is not None:
                try:
                    validate_base_url(
                        declared,
                        allow_insecure_http=bool(entity.allow_insecure_http),
                    )
                except BaseUrlValidationError as exc:
                    blocked.append((KIND_BACKEND, entity.name, f"invalid base_url: {exc}"))
                    continue
            current = current_backends.get(entity.name)
            if current is None:
                if declared is not None and _is_vault_credential(entity):
                    blocked.append((KIND_BACKEND, entity.name, _ENDPOINT_CREATE_CREDENTIAL_REASON))
                continue
            if "base_url" not in current:
                # The response shape predates the endpoint field (FAR-2046), so
                # a declared endpoint would be PATCHed into a server that
                # silently ignores it — a perpetual false-converged drift. Fail
                # loudly instead of reporting a no-op.
                blocked.append((KIND_BACKEND, entity.name, _ENDPOINT_UNSUPPORTED_REASON))
                continue
            if _declared_endpoint_change(entity, current) and _is_vault_credential(entity):
                blocked.append((KIND_BACKEND, entity.name, _ENDPOINT_CHANGE_CREDENTIAL_REASON))
        return blocked

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
            return cast(dict[str, Any], response.json())
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
            return cast(dict[str, Any], response.json())
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
            return cast(dict[str, Any], response.json())
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
            return cast(dict[str, Any], response.json())
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

        The owner-email directory (/admin/users, FAR-1161) is also fetched
        LAZILY — only when a declared pipeline names an owner email — and a
        directory failure is contained the same way: stored as
        ``users_error`` so only the owner-declaring pipelines block with
        that specific reason.

        Triggers are keyed by ``pipeline/name`` (pre-name rows and triggers
        whose pipeline is invisible are unmanaged by apply); the ``agents``
        entry maps agent name -> id for node ref resolution. Duplicate-name
        findings are reported as ``ambiguous_pipeline_names`` /
        ``ambiguous_agent_names`` (name -> match count; ambiguous names are
        EXCLUDED from the maps — name-based upsert cannot pick a row) and
        duplicate live trigger composite keys as ``duplicate_trigger_keys``.

        S3776 split: the pipeline-family lookups/normalisation live in
        :meth:`_fetch_pipeline_bundle` and the trigger composite-key maps
        in :meth:`_fetch_trigger_lists`; the per-kind wants/lazy flags are
        evaluated HERE (never inside the helpers) and passed in.
        """
        schemas_list = self._get_paginated(_PATH_SCHEMAS)
        try:
            backends_list = self._get_paginated(_PATH_MODEL_BACKENDS, params={"include_in_dev": "true"})
        except ApplyHttpError as exc:
            if exc.status_code != 403:
                raise
            _log.warning(
                "apply.fetch_backends_in_dev_forbidden: falling back to default listing (missing in_dev entities)"
            )
            backends_list = self._get_paginated(_PATH_MODEL_BACKENDS)
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

        # Per-kind flags: which kinds the config declares (wants_*) and
        # which lazy per-kind lookups are actually needed (needs_*). All
        # evaluated here so the helpers stay pure fetch/normalise steps.
        wants_pipelines = bool(config.entities.pipelines)
        wants_triggers = bool(config.entities.triggers)
        needs_agents = any(
            entity.graph is not None and any(node.agent is not None for node in entity.graph.nodes)
            for entity in config.entities.pipelines
        )
        needs_users = any(
            entity.business_owner_email or entity.reliability_owner_email for entity in config.entities.pipelines
        )
        needs_evals = any(
            entity.graph is not None and any(node.policy_gate is not None for node in entity.graph.nodes)
            for entity in config.entities.pipelines
        )
        # /pipelines is fetched once here: BOTH helpers need it (graph
        # normalisation reads pipeline ids, the trigger identity map reads
        # pipeline id -> name).
        pipelines_list: list[dict[str, Any]] = []
        if wants_pipelines or wants_triggers:
            pipelines_list = self._get_paginated("/pipelines")

        bundle = self._fetch_pipeline_bundle(
            config,
            pipelines_list=pipelines_list,
            wants_pipelines=wants_pipelines,
            needs_agents=needs_agents,
            needs_users=needs_users,
            needs_evals=needs_evals,
        )
        triggers, duplicate_trigger_keys = self._fetch_trigger_lists(
            wants_triggers=wants_triggers,
            pipelines_list=pipelines_list,
            ambiguous_pipeline_names=bundle.ambiguous_pipeline_names,
        )
        return {
            KIND_SCHEMA: schemas,
            KIND_BACKEND: backends,
            KIND_PIPELINE: bundle.pipelines,
            KIND_TRIGGER: triggers,
            "agents": bundle.agents,
            "users": bundle.users,
            "users_error": bundle.users_error,
            "evals": bundle.evals,
            "evals_error": bundle.evals_error,
            "ambiguous_agent_names": bundle.ambiguous_agent_names,
            "ambiguous_pipeline_names": bundle.ambiguous_pipeline_names,
            "duplicate_trigger_keys": duplicate_trigger_keys,
            "unreadable_graph_pipelines": bundle.unreadable_graph_pipelines,
        }

    def _fetch_pipeline_bundle(
        self,
        config: ApplyConfig,
        *,
        pipelines_list: list[dict[str, Any]],
        wants_pipelines: bool,
        needs_agents: bool,
        needs_users: bool,
        needs_evals: bool,
    ) -> _PipelineBundle:
        """Fetch the pipeline-family lookups and normalise pipeline state.

        ``pipelines_list`` is the shared /pipelines fetch the orchestrator
        already made; ``wants_pipelines``/``needs_*`` are evaluated by the
        orchestrator (per-kind flags never re-derived here).

        /agents is fetched LAZILY: only when a declared pipeline node
        actually references an agent (FAR-681 QA — a graph-less or
        agent-free config never needs the agents list).

        The owner-email directory (/admin/users, FAR-1161) is fetched
        LAZILY only when a declared pipeline names an owner email. A
        directory fetch failure (403 for a non-admin credential, 404, ...)
        is STORED, not raised: only the pipelines that declare an owner
        email are blocked with that specific reason and the rest of the
        config still plans (per-entity containment, mirroring
        unreadable_graph_pipelines). Network errors propagate like every
        other fetch.

        FAR-1109: a declared node policy gate references an eval by UUID.
        The eval map resolves that reference at plan time (a missing eval
        blocks the entity). Fetched LAZILY — only when some declared
        pipeline node carries a ``policy_gate`` — and a fetch failure is
        STORED, not raised: only the gate-declaring pipelines block
        (per-entity containment, mirroring ``users_error`` /
        ``unreadable_graph_pipelines``). The /evals list route already
        filters soft-deleted evals (a soft-deleted eval cannot receive new
        results, so its gate would always resolve 'undefined').
        """
        agents_list: list[dict[str, Any]] = []
        if needs_agents:
            agents_list = self._get_paginated("/agents")
        users: dict[str, str] = {}
        users_error: str | None = None
        if needs_users:
            try:
                users_list = self._get_paginated("/admin/users")
                users = {
                    str(item.get("email")).lower(): str(item["id"])
                    for item in users_list
                    if item.get("email") and item.get("id")
                }
            except ApplyHttpError as exc:
                users_error = str(exc)
                _log.warning("apply.fetch_users_failed: %s", users_error)
        evals: dict[str, dict[str, Any]] = {}
        evals_error: str | None = None
        if needs_evals:
            try:
                evals_list = self._get_paginated(_PATH_EVALS)
                evals = {
                    str(item["id"]): item
                    for item in evals_list
                    if item.get("id") is not None and item.get("deleted_at") is None
                }
            except ApplyHttpError as exc:
                evals_error = str(exc)
                _log.warning("apply.fetch_evals_failed: %s", evals_error)

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
        unreadable_graph_pipelines = self._normalize_pipeline_graphs(
            config,
            pipelines,
            wants_pipelines=wants_pipelines,
        )
        return _PipelineBundle(
            agents=agents,
            ambiguous_agent_names=ambiguous_agent_names,
            pipelines=pipelines,
            ambiguous_pipeline_names=ambiguous_pipeline_names,
            unreadable_graph_pipelines=unreadable_graph_pipelines,
            users=users,
            users_error=users_error,
            evals=evals,
            evals_error=evals_error,
        )

    def _normalize_pipeline_graphs(
        self,
        config: ApplyConfig,
        pipelines: dict[str, dict[str, Any]],
        *,
        wants_pipelines: bool,
    ) -> dict[str, str]:
        """Fetch and normalise the stored graph for each declared pipeline.

        Only pipelines whose config DECLARES a graph need the fetched graph
        (a graph-less config does not manage the graph at all); when the
        config declares no pipelines at all (``wants_pipelines`` false) the
        whole pass is skipped. A stored graph the models reject (legacy data,
        version skew) must not abort the whole run: demote this pipeline to
        blocked ("current graph unreadable") and keep planning the rest of
        the config. Returns name -> reason for each demoted pipeline.
        """
        from modulo.cli.apply import pipeline_apply

        unreadable_graph_pipelines: dict[str, str] = {}
        if not wants_pipelines:
            return unreadable_graph_pipelines
        declared_names = {e.name for e in config.entities.pipelines if e.graph is not None}
        for name in declared_names & pipelines.keys():
            try:
                graph_payload = self._get(f"/pipelines/{pipelines[name]['id']}/graph")
                pipelines[name]["graph"] = pipeline_apply.normalize_current_graph(graph_payload)
            except (ApplyHttpError, ValueError, KeyError, TypeError) as exc:
                reason = str(exc) or exc.__class__.__name__
                _log.warning("apply.fetch_graph_unreadable: %s: %s", name, reason)
                unreadable_graph_pipelines[name] = reason
        return unreadable_graph_pipelines

    def _fetch_trigger_lists(
        self,
        *,
        wants_triggers: bool,
        pipelines_list: list[dict[str, Any]],
        ambiguous_pipeline_names: dict[str, int],
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        """Fetch the trigger list and build the composite-key maps.

        Triggers are keyed by ``pipeline/name`` (pre-name rows and triggers
        whose pipeline is invisible are unmanaged by apply); duplicate live
        trigger composite keys are reported as ``duplicate_trigger_keys``.
        ``pipelines_list``/``ambiguous_pipeline_names`` come from the
        orchestrator's shared /pipelines fetch; ``wants_triggers`` is its
        per-kind flag (evaluated there, not here).
        """
        triggers_list: list[dict[str, Any]] = []
        if wants_triggers:
            triggers_list = self._get_paginated("/triggers")
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
        return triggers, duplicate_trigger_keys

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
                self._apply_schema_entry(status, entry, entities, current_entities, report)

    def _apply_schema_entry(
        self,
        status: str,
        entry: dict[str, Any],
        entities: dict[str, list[tuple[str, Any]]],
        current_entities: dict[str, dict[str, dict[str, Any]]],
        report: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Apply one schema plan entry, containing any failure to that entity."""
        entity = None
        try:
            entity = _find(entities[KIND_SCHEMA], entry["name"])
            assert isinstance(entity, SchemaEntity)
            if status == "created":
                schema_id = self._create_schema(entity)
            else:
                schema_id = self._update_schema(entity, current_entities[KIND_SCHEMA][entity.name])
            existing_versions = (
                {v["version"] for v in current_entities[KIND_SCHEMA][entity.name].get("versions") or []}
                if status == "updated"
                else set()
            )
            self._apply_schema_versions(entity, schema_id, existing_versions, report)
        except (ApplyHttpError, httpx.HTTPError, KeyError, ValueError) as exc:
            message = _failure_message(exc)
            _log.warning("apply schema failed: %s: %s", entity.name if entity else entry["name"], message)
            report[status].remove(entry)
            report["failed"].append({"kind": KIND_SCHEMA, "name": entry["name"], "error": message})

    def _create_schema(self, entity: SchemaEntity) -> Any:
        """POST a new schema and return its server id."""
        response = self._post(
            _PATH_SCHEMAS,
            {
                "name": entity.name,
                "description": entity.description,
                "abstract_name": entity.abstract_name,
            },
        )
        return response["id"]

    def _update_schema(self, entity: SchemaEntity, current: dict[str, Any]) -> Any:
        """PATCH only the changed schema fields; return the existing id."""
        schema_id = current["id"]
        payload: dict[str, Any] = {}
        if entity.description != current.get("description"):
            payload["description"] = entity.description
        if entity.abstract_name != current.get("abstract_name"):
            payload["abstract_name"] = entity.abstract_name
        if payload:
            self._patch(f"/schemas/{schema_id}", payload)
        return schema_id

    def _apply_schema_versions(
        self,
        entity: SchemaEntity,
        schema_id: Any,
        existing_versions: set[Any],
        report: dict[str, list[dict[str, Any]]],
    ) -> None:
        """POST every missing schema version, containing per-version failures."""
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
                self._apply_backend_entry(status, entry, entities, current_entities, resolved_api_keys, report)

    def _apply_backend_entry(
        self,
        status: str,
        entry: dict[str, Any],
        entities: dict[str, list[tuple[str, Any]]],
        current_entities: dict[str, dict[str, dict[str, Any]]],
        resolved_api_keys: dict[str, str],
        report: dict[str, list[dict[str, Any]]],
    ) -> None:
        """Apply one model-backend plan entry, containing any failure."""
        entity = None
        try:
            entity = _find(entities[KIND_BACKEND], entry["name"])
            assert isinstance(entity, ModelBackendEntity)
            if status == "created":
                backend_id = self._create_backend(entity, resolved_api_keys)
            else:
                backend_id = self._update_backend(
                    entity,
                    current_entities[KIND_BACKEND][entity.name],
                    resolved_api_keys,
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

    def _create_backend(self, entity: ModelBackendEntity, resolved_api_keys: dict[str, str]) -> Any:
        """POST a new model backend and return its server id.

        A declared endpoint travels with the create (FAR-2105), in the FAR-2046
        shape the REST ``ModelBackendCreate`` expects: ``base_url`` plus the
        explicit ``allow_insecure_http`` opt-in. An omitted key is simply not
        sent (nothing to manage on a brand-new row).
        """
        payload: dict[str, Any] = {
            "name": entity.name,
            "display_name": entity.display_name,
            "provider": entity.provider,
            "model_id": entity.model_id,
            "api_key": resolved_api_keys[entity.name],
            "default_params": entity.default_params,
            "visibility": entity.visibility,
            "tier": entity.tier,
        }
        _add_declared_endpoint(payload, entity)
        response = self._post(_PATH_MODEL_BACKENDS, payload)
        return response["id"]

    def _update_backend(
        self,
        entity: ModelBackendEntity,
        current: dict[str, Any],
        resolved_api_keys: dict[str, str],
    ) -> Any:
        """PATCH an existing model backend; return its id.

        The endpoint is sent ONLY when the declaration actually changes it
        (FAR-2105): an omitted ``base_url`` is not managed, and a declaration
        that already matches the live endpoint needs no write. A real endpoint
        change also re-sends the literal key, because the FAR-2046 write path
        refuses to move a destination without re-authenticating it (the
        plan-time check has already blocked a vault-reference credential).
        """
        backend_id = current["id"]
        payload: dict[str, Any] = {
            "display_name": entity.display_name,
            "model_id": entity.model_id,
            "default_params": entity.default_params,
            "visibility": entity.visibility,
            "tier": entity.tier,
        }
        if _declared_endpoint_change(entity, current):
            _add_declared_endpoint(payload, entity)
            payload["api_key"] = resolved_api_keys[entity.name]
        self._patch(f"/model-backends/{backend_id}", payload)
        return backend_id

    def run(
        self,
        config: ApplyConfig,
        dry_run: bool,
        refresh_secrets: bool = False,
        drift: bool = False,
    ) -> dict[str, Any]:
        """Resolve refs, plan, optionally execute, and return the report.

        Phase order: schemas -> model_backends -> pipelines -> triggers
        (forward-dependency order; a trigger's pipeline is applied before the
        trigger). Pipeline/trigger resolution failures enter ``blocked`` with
        per-entity containment identical to the slice-1 kinds. With
        ``refresh_secrets`` set, triggers whose raw config declares
        secret-shaped entries always re-send their config, and pipelines
        whose resolved graph declares secret-shaped entries (FAR-1232)
        always re-send their declared graph — secret-only rotation is
        invisible to the drift hash because the server masks stored secrets
        on read.

        With ``drift`` set the run NEVER writes: it returns after the plan
        phase with the report labelled ``"mode": "drift"`` plus the
        node-level pipeline breakdown (``"drift_detail"``) — the same gate
        the dry-run return takes, so no POST/PATCH/PUT code path is
        reachable in drift mode (verified by tests/unit/cli/test_apply_drift.py).
        """
        from modulo.cli.apply import pipeline_apply, trigger_apply

        resolved_api_keys, blocked = resolve_secret_refs(config)
        # FAR-1216: plan-time validation of ``credential_ref`` keys against the
        # org vault — a missing (or unverifiable) key blocks the entity before
        # any write, mirroring the env-ref block. Only declared refs trigger
        # the vault listing call.
        blocked = [*blocked, *self._block_missing_credential_refs(config)]
        entities = _entity_reports(config)
        current_entities: dict[str, Any] = self.fetch_current(config) if any(entities.values()) else _empty_current()
        # FAR-2105: validate declared endpoints and surface the FAR-2046
        # credential-binding rule at PLAN time (before any write), so an
        # unmanageable endpoint never becomes a 422 halfway through an apply.
        blocked = [
            *blocked,
            *self._block_unmanageable_endpoints(
                config,
                current_entities,
                {name for kind, name, _reason in blocked if kind == KIND_BACKEND},
            ),
        ]
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
            config.entities, current_entities, desired, blocked, blocked_keys, refresh_secrets=refresh_secrets
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
        report["failed"] = []
        if dry_run:
            report["dry_run"] = True
            return report
        if drift:
            from modulo.cli.apply.drift import build_drift_detail

            report["mode"] = "drift"
            report["drift_detail"] = build_drift_detail(current_entities, desired, report)
            return report
        self.apply_schemas(entities, current_entities, report)
        self.apply_backends(entities, current_entities, resolved_api_keys, report)
        pipeline_ids = pipeline_apply.apply_pipelines(
            self, entities, current_entities, report, refresh_secrets=refresh_secrets
        )
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
        "users": {},
        "users_error": None,
        "evals": {},
        "evals_error": None,
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


def _format_validation_detail(item: Any) -> str:
    """Render one FastAPI validation-detail entry, preserving its typed code.

    A credential-reference 422 carries ``{"type": "credential_reference_error",
    "loc": [...], "msg": "vault key 'x': ..."}`` (FAR-1640). Flattening it to a
    raw ``repr`` would bury the stable machine-readable ``type`` and the named
    key; this keeps both so apply surfaces the server's typed error verbatim.
    """
    if not isinstance(item, dict):
        return str(item)
    code = item.get("type")
    msg = item.get("msg")
    loc = item.get("loc")
    head_parts = [str(part) for part in (code, msg) if part is not None]
    head = ": ".join(head_parts) if head_parts else str(item)
    if isinstance(loc, list) and loc:
        return f"{head} (loc={'.'.join(str(part) for part in loc)})"
    return head


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
                if isinstance(detail, list):
                    return "; ".join(_format_validation_detail(item) for item in detail)
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
    "VaultListingError",
    "has_blockers",
    "resolve_secret_refs",
]
