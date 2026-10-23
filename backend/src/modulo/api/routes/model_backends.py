"""ModelBackend CRUD REST API.

Credentials (API keys) are encrypted at rest with Fernet. The ciphertext is
never exposed in any response — only a boolean `has_credentials` field.
"""

import asyncio
import ipaddress
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_NOT_FOUND, MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.db_error_handling import (
    handle_db_errors,
    raise_lock_timeout_conflict_if,
    raise_session_contract_error,
)
from modulo.api.dependencies import (
    deny_break_glass_mint,
    deny_break_glass_mint_any_credential,
    get_db_session,
    require_in_dev_operator,
    require_permission,
    require_permission_any_credential,
)
from modulo.api.models.team_visibility import TeamVisibilityMixin
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.secret_storage import decode_stored_secret_scoped
from modulo.auth.team_rbac import org_role_level
from modulo.core.audit_coverage import audited
from modulo.core.audit_logger import append_audit_event_isolated
from modulo.core.model_backend_hub import (
    BASE_URL_CAPABLE_PROVIDERS,
    _build_backend,
    supports_custom_base_url,
)
from modulo.core.model_backend_presets import MODEL_BACKEND_PRESETS
from modulo.core.plugin_registry import get_plugin_registry
from modulo.core.secrets_backend import (
    CredentialReferenceError,
    SecretsBackend,
    create_secrets_backend,
    resolve_credential,
)
from modulo.core.ssrf import validate_outbound_url_syntax
from modulo.core.validation_level import model_backend_baseline_level, resolve_validation_level
from modulo.db.crud.model_backend import (
    create_model_backend,
    delete_model_backend,
    get_model_backend,
    list_backends_referencing_fallback,
    list_model_backends,
    list_pipeline_references_for_backend,
    update_model_backend,
)
from modulo.db.crud.row_lock import set_mutation_row_lock_timeout
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import TERMINAL_STATUSES, Run
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.model_backends.base import HEALTH_CHECK_TIMEOUT
from modulo.settings import Settings, get_settings
from modulo.util import sanitise_log_value as _sanitise_log_value

_CODE_MODEL_BACKENDS_LIST_MODEL = "model_backends.list_model_backends_endpoint"
_MSG_MODEL_BACKENDS_NOT_AVAILABLE = "Model backends are not available. Run database migrations to enable this feature."
_CODE_MODEL_BACKENDS_CREATE_MODEL = "model_backends.create_model_backend_endpoint"
_CODE_MODEL_BACKENDS_GET_MODEL = "model_backends.get_model_backend_endpoint"
_MSG_MODEL_BACKEND_NOT_FOUND = "Model backend not found"
_CODE_MODEL_BACKENDS_UPDATE_MODEL = "model_backends.update_model_backend_endpoint"
_CODE_MODEL_BACKENDS_DELETE_MODEL = "model_backends.delete_model_backend_endpoint"
_CODE_MODEL_BACKENDS_RECHECK_MODEL = "model_backends.recheck_model_backend_health_endpoint"
_CODE_MODEL_BACKENDS_AUDIT_APPEND_FAILED = "model_backends.audit_append_failed"
_PERM_MODEL_BACKEND_LIST = "model_backend.list"
_CODE_MODEL_BACKENDS_PIPELINE_REFS = "model_backends.pipeline_references_endpoint"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/model-backends", tags=["model-backends"])

HealthCheckStatus = Literal["ok", "unhealthy", "not_applicable"]


def _encrypt(api_key: str, fernet_key: str) -> bytes:
    return Fernet(fernet_key.encode()).encrypt(api_key.encode())


# ---------------------------------------------------------------------------
# Custom endpoint (base_url) validation — FAR-2046
# ---------------------------------------------------------------------------
#
# A model backend may point at a self-hosted / OpenAI-compatible gateway on a
# custom host. The endpoint is ENDPOINT CONFIGURATION (stored beside ``api_key``
# in the encrypted credentials JSON), never a model parameter, so credentials
# belong in ``api_key`` and the URL itself must never carry them.

_BASE_URL_MAX_LENGTH = 2048

# Cloud metadata endpoints are a HARD DENY — never reachable by the model
# backend egress path regardless of scheme, hostname or allowlist. 169.254.0.0/16
# is IPv4 link-local (AWS/GCP/Azure IMDS); fe80::/10 is the IPv6 equivalent.
# fd00:ec2::254 is the AWS IMDS IPv6 address, 100.100.100.200 the Aliyun
# endpoint, and ::ffff:169.254.169.254 is the IPv4 address in mapped form (the
# IPv6 network below is checked via ``ipv4_mapped`` too).
_METADATA_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
    ipaddress.ip_network("100.100.100.200/32"),
)
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)


def _base_url_error(message: str, *, code: str | None = None, loc: str = "base_url") -> HTTPException:
    detail: dict[str, Any] = {
        "type": "value_error",
        "loc": ["body", loc],
        "msg": message,
    }
    if code is not None:
        detail["code"] = code
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[detail],
    )


def _is_vault_reference(value: str) -> bool:
    """True for a ``secretref://<key>`` vault reference (resolved server-side)."""
    return value.strip().startswith("secretref://")


def _base_url_literal_credential_error() -> HTTPException:
    """A custom destination requires a literal credential.

    A vault reference is resolved server-side, so the caller may never have seen
    the secret; its owner did not authorise this destination. Accepting a
    reference would let any operator with ``model_backend.update`` (and
    ``secret.manage``, the same role) point a stored secret at their own host.
    """
    return _base_url_error(
        "A custom base_url requires a literal API key; a vault reference is not accepted for a new destination",
        code="base_url_requires_literal_credential",
        loc="api_key",
    )


def _base_url_change_requires_credential_error() -> HTTPException:
    """A destination change must be re-authenticated with a fresh API key.

    Reusing a stored key against a new host would hand that key to an
    operator-supplied destination without the credential owner's action
    (FAR-2046 security finding). The operator permission is unchanged — the
    caller simply has to re-supply the key.
    """
    return _base_url_error(
        "Changing base_url requires the API key to be re-supplied",
        code="base_url_change_requires_credential",
        loc="api_key",
    )


def _reject_metadata_endpoint(host: str) -> None:
    """Raise 422 for a cloud metadata host/literal; never allowlisted."""
    normalized = host.strip("[]").rstrip(".").lower()
    if normalized in _METADATA_HOSTNAMES:
        raise _base_url_error("Base URL must not target a cloud metadata endpoint")
    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(normalized)
    except ValueError:
        return
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if any(address in network for network in _METADATA_NETWORKS if network.version == address.version):
        raise _base_url_error("Base URL must not target a cloud metadata endpoint")


def _validate_base_url(value: str, *, allow_insecure_http: bool = False) -> str:
    """Validate and normalise an operator-supplied model-backend base URL.

    Rules (FAR-2046):
    * ``http``/``https`` only, no embedded userinfo (credentials belong in
      ``api_key``), no query and no fragment;
    * non-canonical IP-literal encodings (decimal/hex integers, mixed-base
      dotted strings) are rejected by the shared outbound-URL syntax floor;
    * cloud metadata endpoints are refused outright;
    * ``https`` is required unless the caller explicitly opts in with
      ``allow_insecure_http`` (a separate, audited flag — never inferred from
      the hostname shape, because a name's privacy cannot be established
      without resolving it).

    Returns the trimmed URL. Raises a typed 422 for any violation.
    """
    candidate = value.strip()
    if not candidate:
        raise _base_url_error("Base URL must not be empty")
    if len(candidate) > _BASE_URL_MAX_LENGTH:
        raise _base_url_error(f"Base URL must be at most {_BASE_URL_MAX_LENGTH} characters")
    try:
        scheme, host = validate_outbound_url_syntax(candidate)
    except ValueError as exc:
        raise _base_url_error(str(exc)) from None
    parsed = urlparse(candidate)
    if parsed.query or parsed.fragment:
        raise _base_url_error("Base URL must not contain a query string or fragment")
    _reject_metadata_endpoint(host)
    if scheme == "http":
        if not allow_insecure_http:
            raise _base_url_error(
                "Base URL must use https://; set allow_insecure_http to permit plain HTTP",
                code="insecure_http_not_allowed",
            )
        logger.warning(
            "model_backends.base_url_http_downgrade host=%s",
            _sanitise_log_value(host),
        )
    return candidate


def _redact_base_url(value: str | None) -> str | None:
    """Reduce a base URL to its host only, for callers who may not manage backends."""
    if not value:
        return None
    try:
        host = urlparse(value).hostname
    except ValueError:
        return None
    return host or None


def _can_manage_model_backends(principal: TenantPrincipal) -> bool:
    """Operator+ may see the full endpoint; viewers/runners only its host."""
    return org_role_level(principal.org_role) >= org_role_level("operator")


def _stored_secret_payload(raw: str | None) -> dict[str, Any]:
    """Parse a stored credential JSON object; malformed/absent -> ``{}``."""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("model_backends.malformed_credential_json")
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _read_secret_payload(
    secrets_backend: SecretsBackend,
    backend_id: uuid.UUID,
) -> dict[str, Any]:
    """Read a backend's stored credential JSON, treating a missing row as empty.

    Legacy rows written before FAR-2046 have a secrets row containing only
    ``api_key``; a row with no secrets entry (the raw-ciphertext fallback) yields
    ``{}`` here and the hub's own fallback still decrypts the API key.
    """
    try:
        raw = await secrets_backend.get_secret(str(backend_id))
    except (KeyError, ValueError):
        return {}
    return _stored_secret_payload(raw)


def _stored_base_url(payload: dict[str, Any]) -> str | None:
    value = payload.get("base_url")
    return value if isinstance(value, str) and value.strip() else None


def _legacy_base_url(default_params: Mapping[str, Any] | None) -> str | None:
    """Return a pre-FAR-2046 ``default_params.base_url``, or ``None``.

    The legacy location is still honoured for existing rows but is never
    written again: create/update migrate it into the credential JSON.
    """
    if not isinstance(default_params, Mapping):
        return None
    value = default_params.get("base_url")
    return value if isinstance(value, str) and value.strip() else None


def _effective_base_url(
    payload: Mapping[str, Any],
    default_params: Mapping[str, Any] | None,
    provider: str | None = None,
) -> str | None:
    """The endpoint the hub will actually use: credential JSON, then legacy.

    ``None`` for a provider that cannot apply a custom endpoint — the legacy
    value in ``default_params`` is ignored by the hub, so reporting it would
    claim egress to a host the row never contacts (FAR-2046).
    """
    if provider is not None and not supports_custom_base_url(provider):
        return None
    return _stored_base_url(dict(payload)) or _legacy_base_url(default_params)


def _requested_base_url(base_url: str | None, default_params: Mapping[str, Any] | None) -> str | None:
    """Fold a legacy ``default_params.base_url`` into the request's base_url.

    A supplied-but-blank value means "not provided" on create / "clear" on
    update (the update path normalises that before calling here). Two different
    values in one request are ambiguous and rejected.
    """
    legacy_raw = default_params.get("base_url") if isinstance(default_params, Mapping) else None
    if legacy_raw is not None and not isinstance(legacy_raw, str):
        raise _base_url_error("default_params.base_url must be a string", loc="default_params")
    legacy = legacy_raw.strip() if isinstance(legacy_raw, str) and legacy_raw.strip() else None
    direct = base_url.strip() if isinstance(base_url, str) and base_url.strip() else None
    if direct is not None and legacy is not None and direct != legacy:
        raise _base_url_error(
            "base_url and default_params.base_url disagree; supply only base_url",
            code="base_url_conflict",
        )
    return direct if direct is not None else legacy


def _strip_legacy_base_url(default_params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return *default_params* without the legacy endpoint key."""
    if not isinstance(default_params, Mapping):
        return {}
    return {key: value for key, value in default_params.items() if key != "base_url"}


def _try_secrets_backend(settings: Settings, session: AsyncSession) -> SecretsBackend | None:
    """Construct the secrets backend for a read path, tolerating failure.

    Reading a backend's endpoint is a best-effort enrichment, so a construction
    failure (e.g. a malformed ``fernet_key``, or an external backend that cannot
    initialise) must degrade to "no credential endpoint shown" — never turn a
    GET / list into a 500. The legacy ``default_params.base_url`` is still
    reported. Logged once per request (fail open WITH a log).
    """
    try:
        return create_secrets_backend(fernet_key=settings.fernet_key, session=session)
    except Exception:
        logger.warning("model_backends.secrets_backend_unavailable", exc_info=True)
        return None


async def _base_urls_for_backends(
    session: AsyncSession,
    settings: Settings,
    backends: list[Any],
) -> dict[uuid.UUID, str | None]:
    """Resolve each row's effective custom endpoint for a read response.

    ``base_url`` lives in the encrypted credential JSON (FAR-2046); a legacy
    ``default_params.base_url`` is still honoured so an un-migrated row reports
    the endpoint it actually uses. A missing/malformed secret is treated as "no
    credential endpoint" rather than failing the whole listing. Runs inside the
    caller's already-open RLS transaction.

    The secrets backend is constructed LAZILY (only once a row that can carry a
    custom endpoint needs a credential read) and construction failure is
    tolerated, so a read never 500s and a page of endpoint-incapable providers
    pays nothing.
    """
    if not backends:
        return {}
    result: dict[uuid.UUID, str | None] = {}
    secrets_backend: SecretsBackend | None = None
    secrets_backend_resolved = False
    for mb in backends:
        provider = getattr(mb, "provider", None)
        if provider is not None and not supports_custom_base_url(provider):
            result[mb.id] = None
            continue
        if not secrets_backend_resolved:
            secrets_backend = _try_secrets_backend(settings, session)
            secrets_backend_resolved = True
        payload: dict[str, Any] = {}
        if secrets_backend is not None:
            payload = await _read_secret_payload(secrets_backend, mb.id)
        result[mb.id] = _effective_base_url(
            payload,
            getattr(mb, "default_params", None),
            provider,
        )
    return result


async def _resolve_credential_or_422(
    session: AsyncSession,
    settings: Settings,
    *,
    value: str,
) -> tuple[str, SecretsBackend]:
    """Resolve a literal/``secretref://`` credential (FAR-1640).

    Runs under the caller's already-opened RLS transaction (the backend reads
    the org from the session), so a key belonging to another organisation is
    indistinguishable from a missing one. Every resolution failure is a typed
    422 naming the offending key — never a silent empty credential, never a 500.
    Returns the resolved value plus the backend instance (reused for the
    encrypted write).
    """
    secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
    try:
        resolved = await resolve_credential(secrets_backend, value)
    except CredentialReferenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.validation_detail(["body", "api_key"]),
        ) from None
    return resolved, secrets_backend


async def _run_health_check_on_save(
    provider: str,
    model_id: str,
    api_key: str | None,
    default_params: dict[str, Any],
    base_url: str | None = None,
) -> tuple[HealthCheckStatus, str | None]:
    """Best-effort test-inference health check run on save (PRD §8.1 ``health_check``).

    Builds the configured backend and runs its health check — a ``GET /models``
    ping for OpenAI-compatible providers, an inference call for the rest — so
    auth failures and quota errors are surfaced before pipelines reference the
    backend. The result is recorded on the entity's ``last_health_check_at`` /
    ``last_health_check_error`` columns, which the graph validator surfaces as
    ``MODEL_BACKEND_UNHEALTHY`` at save/run time.

    Returns ``(status, detail)`` where *detail* is ``None`` on success:

    * ``"ok"`` — the provider responded; the credentials are valid.
    * ``"unhealthy"`` — the provider responded with a failure (auth, quota,
      network, timeout); *detail* carries the provider error.
    * ``"not_applicable"`` — the provider cannot be built from the API-supplied
      credentials alone (e.g. Bedrock needs aws keys, vertexai needs ``project``,
      watsonx needs ``project_id``, azure_openai needs ``azure_endpoint``). This
      is a configuration limitation, NOT a health failure — the caller must not
      record an error, or the graph validator would hard-block every run.

    Never raises and never blocks the create/update: the check is best-effort
    (a transient provider outage must not prevent configuring a backend).
    """
    try:
        creds: dict[str, Any] = {"api_key": api_key} if api_key else {}
        if base_url:
            creds["base_url"] = base_url
        backend = _build_backend(provider, model_id, creds, default_params)
    except HTTPException:
        raise
    except Exception:
        # Provider cannot be constructed from API-supplied credentials — not a
        # health failure. Never persisted as last_health_check_error (the graph
        # validator would surface it as MODEL_BACKEND_UNHEALTHY on every run).
        return "not_applicable", None
    try:
        result = await asyncio.wait_for(backend.health_check(), timeout=HEALTH_CHECK_TIMEOUT)
        if result.ok:
            return "ok", None
        return "unhealthy", result.detail
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception as exc:
        return "unhealthy", str(exc)[:500]


async def _persist_health_check_result(
    session: AsyncSession,
    backend_id: uuid.UUID,
    status_: HealthCheckStatus,
    detail: str | None,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    org_role: str,
) -> None:
    """Persist a health-check result on the entity in a short transaction.

    Called AFTER the request's write transaction has committed, so the provider
    network call that produced the result never held the DB connection or a row
    lock (the check runs with ``HEALTH_CHECK_TIMEOUT`` seconds of budget).
    ``not_applicable`` records no error (and clears any stale one) — a provider
    the API cannot construct must never block runs as ``MODEL_BACKEND_UNHEALTHY``.
    """
    checked_at = datetime.now(UTC)
    async with session.begin():
        await set_rls_org(session, org_id)
        await set_rls_user_context(session, user_id, org_role)
        row = await session.get(ModelBackend, backend_id)
        if row is None:
            return
        row.last_health_check_at = checked_at
        row.last_health_check_error = None if status_ != "unhealthy" else detail
        row.validation_level = resolve_validation_level(
            model_backend_baseline_level(row.provider),
            last_health_check_at=checked_at,
            last_health_check_error=None if status_ != "unhealthy" else detail,
        )


async def _run_health_check_on_save_and_persist(
    session: AsyncSession,
    backend: ModelBackend,
    provider: str,
    model_id: str,
    api_key: str | None,
    default_params: dict[str, Any],
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    org_role: str,
    base_url: str | None = None,
) -> tuple[HealthCheckStatus, str | None]:
    """Run the save-time health check outside the write transaction and persist it.

    Best-effort end to end: the entity write has already committed before this
    runs, so a check or persistence failure must not fail the request — the
    result is logged, not propagated.
    """
    status_, detail = await _run_health_check_on_save(provider, model_id, api_key, default_params, base_url)
    try:
        await _persist_health_check_result(
            session,
            backend.id,
            status_,
            detail,
            org_id=org_id,
            user_id=user_id,
            org_role=org_role,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to persist health check result for model backend %s", backend.id)
    return status_, detail


def _audit_safe_backend_fields(updates: dict[str, Any]) -> dict[str, Any]:
    """Return the non-credential update fields, UUID values stringified, for audit payloads."""
    safe: dict[str, Any] = {}
    for key, value in updates.items():
        if key == "credentials_ciphertext":
            continue
        if isinstance(value, uuid.UUID):
            safe[key] = str(value)
        else:
            safe[key] = value
    return safe


async def _list_snapshots_referencing_backend(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    backend_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Return ``{snapshot_id, pipeline_id}`` for every org snapshot whose
    ``model_backend_pins_json`` references ``backend_id`` and that is tied to a
    non-terminal run.

    PRD §8.1 deletion protection — a backend referenced by a PipelineSnapshot
    associated with a non-terminal run cannot be hard-deleted (the run may
    still need the pinned backend, e.g. after a HITL pause/resume). Snapshots
    referenced only by terminal runs impose no such constraint. Pins are a JSON
    column with no relational FK, so the org's snapshots are scanned in Python
    for a ``model_backend_id`` match, mirroring ``list_backends_referencing_fallback``.
    """
    target = str(backend_id)
    stmt = (
        select(PipelineSnapshot)
        .join(Run, Run.snapshot_id == PipelineSnapshot.id)
        .where(
            PipelineSnapshot.organisation_id == org_id,
            Run.status.not_in(TERMINAL_STATUSES),
        )
    )
    matches: list[dict[str, Any]] = []
    rows = (await session.execute(stmt)).scalars()
    for snap in rows:
        pins = snap.model_backend_pins_json or []
        if any(str(pin.get("model_backend_id")) == target for pin in pins):
            matches.append({"snapshot_id": snap.id, "pipeline_id": snap.pipeline_id})
    return matches


class ModelBackendCreate(TeamVisibilityMixin):
    name: str = Field(..., min_length=1, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    provider: str = Field(..., min_length=1, max_length=128)
    model_id: str = Field(..., min_length=1, max_length=128)
    # A literal credential, or a ``secretref://<key>`` vault reference the
    # SERVER resolves at write time (FAR-1640).
    api_key: str = Field(..., min_length=1)
    # Optional custom endpoint (FAR-2046): a self-hosted / OpenAI-compatible
    # gateway. Endpoint configuration, persisted beside ``api_key`` in the
    # encrypted credentials JSON — never a model parameter.
    base_url: str | None = Field(None, max_length=_BASE_URL_MAX_LENGTH)
    # Explicit opt-in to plain HTTP for an air-gapped internal gateway. Kept
    # separate from the URL so the downgrade is auditable and is never inferred
    # from a hostname's shape.
    allow_insecure_http: bool | None = None
    # A REAL field (not ClassVar): pydantic v2 excludes ClassVar annotations
    # from the model fields, so a ClassVar default_params silently dropped
    # every POST body's default_params on the floor (FAR-681 apply round-trip).
    default_params: dict[str, Any] = Field(default_factory=dict)
    visibility: str = Field(default="org")
    owner_team_id: uuid.UUID | None = None
    fallback_backend_ids: list[uuid.UUID] | None = None
    tier: Literal["native", "preview", "in_dev"] = Field(default="native")


class ModelBackendUpdate(TeamVisibilityMixin):
    name: str | None = Field(None, min_length=1, max_length=255)
    display_name: str | None = Field(None, min_length=1, max_length=255)
    model_id: str | None = Field(None, min_length=1, max_length=128)
    api_key: str | None = Field(None, min_length=1)
    # Optional endpoint override. An explicit ``null`` clears a stored base_url;
    # omitting the field leaves it unchanged.
    base_url: str | None = Field(None, max_length=_BASE_URL_MAX_LENGTH)
    allow_insecure_http: bool | None = None
    default_params: dict[str, Any] | None = None
    visibility: str | None = None
    owner_team_id: uuid.UUID | None = None
    fallback_backend_ids: list[uuid.UUID] | None = None
    tier: Literal["native", "preview", "in_dev"] | None = None


class ModelBackendResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    display_name: str
    provider: str
    model_id: str
    has_credentials: bool
    # The custom endpoint, if configured. For a caller who may not manage
    # model backends this is redacted to the host only (never the full URL).
    base_url: str | None = None
    default_params: dict[str, Any]
    visibility: str
    owner_team_id: uuid.UUID | None = None
    tier: str
    fallback_backend_ids: list[uuid.UUID] | None = None
    created_by: uuid.UUID = Field(validation_alias="account_id")
    created_at: datetime
    updated_at: datetime
    validation_level: str | None = None

    model_config = {"from_attributes": True, "populate_by_name": True}


class ModelBackendListResponse(BaseModel):
    items: list[ModelBackendResponse]
    total: int
    page: int
    page_size: int


class ModelBackendHealthCheckResponse(BaseModel):
    status: Literal["healthy", "unhealthy", "not_applicable"]
    detail: str | None = None
    checked_at: datetime | None = None


class PipelineReference(BaseModel):
    pipeline_id: uuid.UUID
    pipeline_name: str
    agent_name: str | None = None
    agent_id: uuid.UUID | None = None
    reference_type: Literal["direct_node", "agent"]


class PipelineReferenceListResponse(BaseModel):
    items: list[PipelineReference]
    total: int
    page: int
    page_size: int


class ModelBackendPresetResponse(BaseModel):
    id: str
    provider: str
    display_name: str
    default_model_id: str
    description: str
    api_key_docs_url: str
    default_params: dict[str, Any]


class ModelBackendPresetListResponse(BaseModel):
    items: list[ModelBackendPresetResponse]
    # FAR-2046: the providers that can consume a custom base_url (the hub's
    # capability map is the single source of truth). The UI shows the Base URL
    # field only for these.
    base_url_providers: list[str] = Field(default_factory=list)


def _to_response(
    mb: Any,
    *,
    base_url: str | None = None,
    can_manage: bool = True,
) -> ModelBackendResponse:
    raw_fallback_ids = getattr(mb, "fallback_backend_ids", None)
    fallback_ids: list[uuid.UUID] | None = None
    if raw_fallback_ids is not None:
        fallback_ids = [uuid.UUID(fid) if isinstance(fid, str) else fid for fid in raw_fallback_ids]
    default_params = dict(mb.default_params or {})
    response_base_url = base_url
    provider = getattr(mb, "provider", None)
    if provider is not None and not supports_custom_base_url(provider):
        # The hub cannot use this legacy value; never report it as configured.
        default_params.pop("base_url", None)
    elif not can_manage:
        response_base_url = _redact_base_url(base_url)
        legacy = default_params.get("base_url")
        if isinstance(legacy, str):
            default_params["base_url"] = _redact_base_url(legacy)
    return ModelBackendResponse(
        id=mb.id,
        organisation_id=mb.organisation_id,
        name=mb.name,
        display_name=mb.display_name,
        provider=mb.provider,
        model_id=mb.model_id,
        has_credentials=bool(mb.credentials_ciphertext),
        base_url=response_base_url,
        default_params=default_params,
        visibility=mb.visibility,
        owner_team_id=mb.owner_team_id,
        tier=mb.tier,
        fallback_backend_ids=fallback_ids,
        created_by=mb.account_id,
        created_at=mb.created_at,
        updated_at=mb.updated_at,
        validation_level=getattr(mb, "validation_level", None),
    )


@router.get("", responses={401: {"description": "Unauthorized"}})
@handle_db_errors(_CODE_MODEL_BACKENDS_LIST_MODEL)
async def list_model_backends_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_in_dev: bool = Query(default=False, description="Include in_dev tier items (default excludes them)"),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    # any_credential: declarative apply (FAR-681) and CI/CD list this resource
    # with mk_ org API keys; roles are clamped to the key's live membership.
    principal: TenantPrincipal = require_permission_any_credential(_PERM_MODEL_BACKEND_LIST),
) -> ModelBackendListResponse:
    if include_in_dev:
        require_in_dev_operator(principal, "model_backend.list.in_dev")
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_model_backends(
                session,
                org_id=principal.organisation_id,
                page=page,
                page_size=page_size,
                excluded_tiers=[] if include_in_dev else None,
            )
            base_urls = await _base_urls_for_backends(session, settings, result.items)
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_LIST_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_LIST_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "model_backends.list_model_backends_endpoint")
        logger.exception(_CODE_MODEL_BACKENDS_LIST_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while listing model backends.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error listing model backends")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing model backends.",
        ) from None
    can_manage = _can_manage_model_backends(principal)
    return ModelBackendListResponse(
        items=[_to_response(mb, base_url=base_urls.get(mb.id), can_manage=can_manage) for mb in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


_CODE_MODEL_BACKENDS_PRESETS = "model_backends.presets_endpoint"
_MSG_MODEL_BACKENDS_PRESETS_NOT_AVAILABLE = "Model backend presets are not available."


@router.get("/presets", responses={401: {"description": "Unauthorized"}})
@handle_db_errors(_CODE_MODEL_BACKENDS_PRESETS)
async def list_model_backend_presets_endpoint(
    principal: TenantPrincipal = require_permission_any_credential(_PERM_MODEL_BACKEND_LIST),
) -> ModelBackendPresetListResponse:
    """Return the curated provider presets for the quick-start create flow."""
    try:
        return ModelBackendPresetListResponse(
            items=[ModelBackendPresetResponse(**preset.model_dump()) for preset in MODEL_BACKEND_PRESETS],
            base_url_providers=sorted(BASE_URL_CAPABLE_PROVIDERS),
        )
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_PRESETS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_PRESETS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_PRESETS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "model_backends.list_model_backend_presets_endpoint")
        logger.exception(_CODE_MODEL_BACKENDS_PRESETS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching model backend presets.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error fetching model backend presets")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching model backend presets.",
        ) from None


_VALID_PROVIDERS = {
    "ai21",
    "anthropic",
    "azure_openai",
    "bedrock",
    "cohere",
    "custom",
    "deepseek",
    "fireworks",
    "gemini",
    "grok",
    "groq",
    "jan",
    "llamacpp",
    "lm_studio",
    "localai",
    "mistral",
    "ollama",
    "opencode",
    "openai",
    "openrouter",
    "perplexity",
    "qwen",
    "replicate",
    "tgi",
    "togetherai",
    "vertexai",
    "vllm",
    "watsonx",
}


async def _validate_fallback_ids(
    session: AsyncSession,
    org_id: uuid.UUID,
    fallback_ids: list[uuid.UUID],
    *,
    backend_id: uuid.UUID | None = None,
) -> None:
    """Reject fallback backend IDs that reference no existing org backend (422).

    The ``fallback_backend_ids`` JSON column has no relational FK (PRD 8.1), so
    the API is the enforcement point: a fallback chain pointing at a backend
    that does not exist (or belongs to another org) is a configuration error,
    not a runtime-failover concern. The hub already skips unregistered IDs
    gracefully, but a silent skip hides a misconfiguration — fail fast instead.

    On update, a self-reference (the backend listing itself as its own
    fallback) is also rejected: it is a meaningless failover chain and would
    permanently block deletion (the delete-protection scan reports the backend
    as referencing itself).
    """
    if not fallback_ids:
        return
    unique_ids = list(dict.fromkeys(fallback_ids))
    if backend_id is not None and backend_id in unique_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[
                {
                    "type": "value_error",
                    "loc": ["body", "fallback_backend_ids"],
                    "msg": "A model backend cannot reference itself as a fallback",
                }
            ],
        )
    found = set(
        (
            await session.execute(
                select(ModelBackend.id).where(
                    ModelBackend.organisation_id == org_id,
                    ModelBackend.id.in_(unique_ids),
                )
            )
        ).scalars()
    )
    missing = [fid for fid in unique_ids if fid not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[
                {
                    "type": "value_error",
                    "loc": ["body", "fallback_backend_ids"],
                    "msg": "Unknown model backend id(s) referenced as fallbacks: "
                    + ", ".join(str(fid) for fid in missing),
                }
            ],
        )


def _validate_provider(provider: str) -> None:
    """Raise 422 if provider is not a known built-in or plugin backend."""
    if provider in _VALID_PROVIDERS:
        return
    try:
        registry = get_plugin_registry()
        if registry.has_model_backend(provider):
            return
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("model_backends._validate_provider")
        logger.warning("Plugin registry check failed for provider %r: %s", _sanitise_log_value(provider), exc)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[
            {
                "type": "value_error",
                "loc": ["body", "provider"],
                "msg": f"Unknown model backend provider: {provider!r}",
            }
        ],
    )


def _resolve_create_base_url(req: ModelBackendCreate) -> str | None:
    """Validate an optional custom endpoint on a create request (FAR-2046)."""
    requested_base_url = _requested_base_url(req.base_url, req.default_params)
    base_url = (
        _validate_base_url(requested_base_url, allow_insecure_http=bool(req.allow_insecure_http))
        if requested_base_url
        else None
    )
    if base_url is not None and not supports_custom_base_url(req.provider):
        raise _base_url_error(
            f"Provider {req.provider!r} does not support a custom base_url",
            code="base_url_not_supported_for_provider",
            loc="provider",
        )
    if base_url is not None and _is_vault_reference(req.api_key):
        raise _base_url_literal_credential_error()
    return base_url


def _requested_fallback_ids(req: ModelBackendCreate) -> list[str] | None:
    """Stringify the fallback ids for the JSON column, or None when unset."""
    if not req.fallback_backend_ids:
        return None
    return [str(fid) for fid in req.fallback_backend_ids]


async def _create_backend_in_transaction(
    session: AsyncSession,
    req: ModelBackendCreate,
    principal: TenantPrincipal,
    settings: Settings,
    default_params: dict[str, Any],
    base_url: str | None,
) -> tuple[ModelBackend, str]:
    """Write the row and its credential in one transaction (FAR-1825/1640)."""
    async with session.begin():
        # FAR-1825: bound the creation-name uniqueness lock below (the
        # transaction's first row lock). A contended same-name row must
        # surface 55P03 -> 409, never an unbounded request wait.
        await set_mutation_row_lock_timeout(session)
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)

        # FAR-1640: resolve a ``secretref://<key>`` vault reference under
        # the caller's org RLS context BEFORE anything is written — a
        # missing/foreign key is a typed 422.
        resolved_api_key, secrets_backend = await _resolve_credential_or_422(session, settings, value=req.api_key)
        ciphertext = _encrypt(resolved_api_key, settings.fernet_key)

        if req.fallback_backend_ids:
            await _validate_fallback_ids(session, principal.organisation_id, req.fallback_backend_ids)

        # C4 (FAR-1825): the uniqueness SELECT ... FOR UPDATE is this
        # transaction's first row lock; the bound above keeps a contended row
        # from parking the connection.
        existing = (
            await session.execute(
                select(ModelBackend)
                .where(
                    ModelBackend.organisation_id == principal.organisation_id,
                    ModelBackend.name == req.name,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A model backend with name {req.name!r} already exists in this organisation",
            )

        mb = await create_model_backend(
            session,
            org_id=principal.organisation_id,
            name=req.name,
            display_name=req.display_name,
            provider=req.provider,
            model_id=req.model_id,
            credentials_ciphertext=ciphertext,
            account_id=principal.account_id,
            default_params=default_params,
            visibility=req.visibility,
            owner_team_id=req.owner_team_id,
            fallback_backend_ids=_requested_fallback_ids(req),
            tier=req.tier,
        )

        secret_value = json.dumps({"api_key": resolved_api_key, **({"base_url": base_url} if base_url else {})})
        await secrets_backend.set_secret(str(mb.id), secret_value)
        return mb, resolved_api_key


async def _audit_created_backend(
    session: AsyncSession,
    principal: TenantPrincipal,
    mb: ModelBackend,
    req: ModelBackendCreate,
    base_url: str | None,
) -> None:
    """Append the PRD §8.12 registration audit event (failure-isolated)."""
    await append_audit_event_isolated(
        session,
        principal,
        resource_type="model_backend",
        event_type="model_backend.created",
        resource_id=mb.id,
        payload={
            "name": mb.name,
            "provider": mb.provider,
            "model_id": mb.model_id,
            "tier": mb.tier,
            "fallback_backend_ids": [str(fid) for fid in (mb.fallback_backend_ids or [])],
            "has_credentials": bool(mb.credentials_ciphertext),
            "has_custom_base_url": base_url is not None,
            "allow_insecure_http": bool(base_url and base_url.lower().startswith("http://")),
            "credential_kind": "vault_reference" if _is_vault_reference(req.api_key) else "literal",
        },
        log_key=_CODE_MODEL_BACKENDS_AUDIT_APPEND_FAILED,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(deny_break_glass_mint_any_credential),
        Depends(audited("model_backend_created", "model_backend", principal_dep=get_current_tenant_user_or_api_key)),
    ],
)
@handle_db_errors(_CODE_MODEL_BACKENDS_CREATE_MODEL)
async def create_model_backend_endpoint(
    req: ModelBackendCreate,
    session: AsyncSession = Depends(get_db_session),
    # any_credential: declarative apply (FAR-681) creates backends with mk_ keys.
    principal: TenantPrincipal = require_permission_any_credential("model_backend.create"),
    settings: Settings = Depends(get_settings),
) -> ModelBackendResponse:
    _validate_provider(req.provider)
    base_url = _resolve_create_base_url(req)
    default_params = _strip_legacy_base_url(req.default_params)
    try:
        mb, resolved_api_key = await _create_backend_in_transaction(
            session,
            req,
            principal,
            settings,
            default_params,
            base_url,
        )
        response = _to_response(mb, base_url=base_url, can_manage=True)
        # The entity write has COMMITTED above. The PRD 8.1 health check runs
        # OUTSIDE the write transaction so the provider network call (up to
        # HEALTH_CHECK_TIMEOUT=10s) never holds the DB connection or a row
        # lock; the result is persisted in a short second transaction.
        await _run_health_check_on_save_and_persist(
            session,
            mb,
            req.provider,
            req.model_id,
            resolved_api_key,
            default_params,
            org_id=principal.organisation_id,
            user_id=principal.account_id,
            org_role=principal.org_role,
            base_url=base_url,
        )
        # PRD §8.12 audit trail: backend registration was previously invisible.
        # Written in a fresh transaction (the create above already committed)
        # and failure-isolated so a broken append never fails a completed create.
        await _audit_created_backend(session, principal, mb, req, base_url)
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_CREATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_CREATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        # FAR-1825: the create transaction bounds its first row lock (the
        # same-name uniqueness SELECT ... FOR UPDATE), so 55P03 is reachable
        # here and must be a 409 conflict, not the generic 503 below.
        raise_session_contract_error(exc, _CODE_MODEL_BACKENDS_CREATE_MODEL)
        raise_lock_timeout_conflict_if(exc, _CODE_MODEL_BACKENDS_CREATE_MODEL)
        logger.exception(_CODE_MODEL_BACKENDS_CREATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while creating model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error creating model backend")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while creating model backend.",
        ) from None
    return response


@router.get("/{backend_id}")
@handle_db_errors(_CODE_MODEL_BACKENDS_GET_MODEL)
async def get_model_backend_endpoint(
    backend_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    principal: TenantPrincipal = require_permission(_PERM_MODEL_BACKEND_LIST),
) -> ModelBackendResponse:
    base_url: str | None = None
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mb = await get_model_backend(session, backend_id)
            # FAR-2046: authorise BEFORE resolving the endpoint. The endpoint
            # lives in the credential JSON, so a cross-org read must be a 404
            # before any decryption happens — never a 500 from the secrets
            # backend, and never a reason to touch another org's secret.
            if mb is None or mb.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MODEL_BACKEND_NOT_FOUND)
            base_url = (await _base_urls_for_backends(session, settings, [mb])).get(mb.id)
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_GET_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_GET_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "model_backends.get_model_backend_endpoint")
        logger.exception(_CODE_MODEL_BACKENDS_GET_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error fetching model backend")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching model backend.",
        ) from None
    return _to_response(mb, base_url=base_url, can_manage=_can_manage_model_backends(principal))


@router.get("/{backend_id}/pipeline-references")
@handle_db_errors(_CODE_MODEL_BACKENDS_PIPELINE_REFS)
async def list_pipeline_references_endpoint(
    backend_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_PERM_MODEL_BACKEND_LIST),
) -> PipelineReferenceListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mb = await get_model_backend(session, backend_id)
            if mb is None or mb.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MODEL_BACKEND_NOT_FOUND)
            result = await list_pipeline_references_for_backend(
                session,
                org_id=principal.organisation_id,
                backend_id=backend_id,
                page=page,
                page_size=page_size,
            )
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_PIPELINE_REFS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_PIPELINE_REFS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "model_backends.list_pipeline_references_endpoint")
        logger.exception(_CODE_MODEL_BACKENDS_PIPELINE_REFS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while listing pipeline references.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error listing pipeline references")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while listing pipeline references.",
        ) from None
    return PipelineReferenceListResponse(
        items=[PipelineReference(**ref) for ref in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


def _prepare_update_payload(req: ModelBackendUpdate) -> dict[str, Any]:
    """Build the non-credential update dict.

    ``api_key`` is removed and handled inside the RLS transaction (FAR-1640),
    where a ``secretref://`` vault reference can be resolved under the caller's
    org context and the resolved value encrypted exactly as before.
    """
    updates: dict[str, Any] = req.model_dump(exclude_unset=True)
    updates.pop("api_key", None)
    # base_url/allow_insecure_http are endpoint configuration in the encrypted
    # credential JSON, not database columns (FAR-2046) — handled inside the
    # transaction beside api_key.
    updates.pop("base_url", None)
    updates.pop("allow_insecure_http", None)
    return updates


async def _stringify_fallback_ids(
    session: AsyncSession,
    org_id: uuid.UUID,
    updates: dict[str, Any],
    backend_id: uuid.UUID,
) -> None:
    """Validate fallback ids and stringify them for the JSON column.

    JSON column cannot serialize raw uuid.UUID objects; stringify before the
    write, mirroring the create path.
    """
    fallback_ids = updates.get("fallback_backend_ids")
    if fallback_ids is not None:
        await _validate_fallback_ids(session, org_id, fallback_ids, backend_id=backend_id)
        updates["fallback_backend_ids"] = [str(fid) for fid in fallback_ids]


@dataclass
class _UpdateContext:
    """State loaded once for a model-backend update (FAR-2046)."""

    existing: ModelBackend
    secrets_backend: SecretsBackend
    secret_payload: dict[str, Any]
    credential_base_url: str | None
    existing_legacy: str | None
    current_base_url: str | None
    credential_present: bool


@dataclass(frozen=True)
class _EndpointUpdate:
    """Resolved endpoint change for one update request (FAR-2046)."""

    update: bool
    requested_base_url: str | None
    changed: bool


async def _load_update_state(
    session: AsyncSession,
    backend_id: uuid.UUID,
    principal: TenantPrincipal,
    req: ModelBackendUpdate,
    settings: Settings,
    updates: dict[str, Any],
) -> _UpdateContext:
    """Load the row, resolve its stored endpoint and read the secret payload."""
    existing = await get_model_backend(session, backend_id)
    if existing is None or existing.organisation_id != principal.organisation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_NOT_FOUND)

    if req.default_params is not None:
        # Never persist the legacy endpoint key; it is migrated into the
        # credential JSON (FAR-2046).
        updates["default_params"] = _strip_legacy_base_url(req.default_params)

    secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
    secret_payload = await _read_secret_payload(secrets_backend, backend_id)
    credential_base_url = _stored_base_url(secret_payload)
    existing_legacy = _legacy_base_url(existing.default_params)
    current_base_url = (credential_base_url or existing_legacy) if supports_custom_base_url(existing.provider) else None
    credential_present = bool(existing.credentials_ciphertext) or isinstance(secret_payload.get("api_key"), str)
    return _UpdateContext(
        existing=existing,
        secrets_backend=secrets_backend,
        secret_payload=secret_payload,
        credential_base_url=credential_base_url,
        existing_legacy=existing_legacy,
        current_base_url=current_base_url,
        credential_present=credential_present,
    )


def _is_legacy_echo(
    req: ModelBackendUpdate,
    ctx: _UpdateContext,
    legacy_supplied: bool,
) -> bool:
    """True when the client echoed an unchanged legacy endpoint it cannot use.

    The pre-FAR-2046 UI echoed the stored ``default_params.base_url`` for every
    provider, including ones that cannot apply it (e.g. azure_openai). Treating
    that echo as a newly-supplied endpoint would reject a harmless edit.
    """
    return (
        legacy_supplied
        and ctx.existing_legacy is not None
        and _legacy_base_url(req.default_params) == ctx.existing_legacy
        and not supports_custom_base_url(ctx.existing.provider)
    )


def _should_preserve_legacy(
    req: ModelBackendUpdate,
    ctx: _UpdateContext,
    *,
    base_url_supplied: bool,
    legacy_supplied: bool,
) -> bool:
    """True when replacing default_params must migrate the legacy endpoint.

    Replacing ``default_params`` without carrying the legacy key forward would
    silently reset a stored endpoint (FAR-2046). The legacy value is only the
    effective endpoint when the credential JSON has none of its own.
    """
    return (
        ctx.existing_legacy is not None
        and ctx.credential_base_url is None
        and req.default_params is not None
        and not base_url_supplied
        and not legacy_supplied
        and supports_custom_base_url(ctx.existing.provider)
    )


def _direct_update_base_url(req: ModelBackendUpdate, legacy: str | None) -> str | None:
    """Validate a ``base_url`` supplied directly on the update request."""
    direct = req.base_url.strip() if isinstance(req.base_url, str) and req.base_url.strip() else None
    if direct is not None and legacy is not None and direct != legacy:
        raise _base_url_error(
            "base_url and default_params.base_url disagree; supply only base_url",
            code="base_url_conflict",
        )
    return _validate_base_url(direct, allow_insecure_http=bool(req.allow_insecure_http)) if direct else None


def _requested_update_base_url(
    req: ModelBackendUpdate,
    ctx: _UpdateContext,
    *,
    base_url_supplied: bool,
    legacy_supplied: bool,
) -> str | None:
    """Resolve and validate the endpoint the update asks to store."""
    if base_url_supplied:
        legacy = _legacy_base_url(req.default_params) if legacy_supplied else None
        return _direct_update_base_url(req, legacy)
    # Without a supplied legacy value the only other way to request an endpoint
    # update is migrating the stored legacy value (preserve_legacy).
    raw = _legacy_base_url(req.default_params) if legacy_supplied else ctx.existing_legacy
    return _validate_base_url(raw, allow_insecure_http=bool(req.allow_insecure_http)) if raw else None


def _finish_endpoint_update(
    req: ModelBackendUpdate,
    ctx: _UpdateContext,
    requested_base_url: str | None,
) -> _EndpointUpdate:
    """Apply the endpoint capability and credential-rebinding rules."""
    if requested_base_url is not None and not supports_custom_base_url(ctx.existing.provider):
        raise _base_url_error(
            f"Provider {ctx.existing.provider!r} does not support a custom base_url",
            code="base_url_not_supported_for_provider",
            loc="provider",
        )
    endpoint_changed = requested_base_url != ctx.current_base_url
    if endpoint_changed:
        # A destination change must be paired with a literal credential: a
        # vault reference or a reused stored key would send the caller's secret
        # to a new host on the post-save health check.
        if req.api_key is not None and _is_vault_reference(req.api_key):
            raise _base_url_literal_credential_error()
        if ctx.credential_present and req.api_key is None:
            raise _base_url_change_requires_credential_error()
    return _EndpointUpdate(update=True, requested_base_url=requested_base_url, changed=endpoint_changed)


def _resolve_endpoint_update(req: ModelBackendUpdate, ctx: _UpdateContext) -> _EndpointUpdate:
    """Determine whether the request changes the stored endpoint."""
    base_url_supplied = "base_url" in req.model_fields_set
    legacy_supplied = isinstance(req.default_params, Mapping) and "base_url" in req.default_params
    legacy_echo = _is_legacy_echo(req, ctx, legacy_supplied)
    preserve_legacy = _should_preserve_legacy(
        req,
        ctx,
        base_url_supplied=base_url_supplied,
        legacy_supplied=legacy_supplied,
    )
    endpoint_update = base_url_supplied or (legacy_supplied and not legacy_echo) or preserve_legacy
    if not endpoint_update:
        return _EndpointUpdate(update=False, requested_base_url=None, changed=False)
    requested_base_url = _requested_update_base_url(
        req,
        ctx,
        base_url_supplied=base_url_supplied,
        legacy_supplied=legacy_supplied,
    )
    return _finish_endpoint_update(req, ctx, requested_base_url)


async def _recover_stored_api_key(
    session: AsyncSession,
    backend_id: uuid.UUID,
    principal: TenantPrincipal,
    settings: Settings,
    ctx: _UpdateContext,
) -> None:
    """Seed the credential JSON from a legacy ciphertext-only row.

    An endpoint-only edit must never replace the key with a base_url-only
    secret row (FAR-2046), so recover the key before writing the endpoint.
    """
    if isinstance(ctx.secret_payload.get("api_key"), str) or not ctx.existing.credentials_ciphertext:
        return
    try:
        recovered = await decode_stored_secret_scoped(
            session,
            ctx.existing.credentials_ciphertext,
            settings.fernet_key,
            org_id=principal.organisation_id,
        )
    except Exception:
        logger.warning("Could not recover stored credentials for model backend %s during update", backend_id)
    else:
        ctx.secret_payload["api_key"] = recovered


async def _apply_api_key_update(
    session: AsyncSession,
    req: ModelBackendUpdate,
    settings: Settings,
    updates: dict[str, Any],
    ctx: _UpdateContext,
) -> str | None:
    """Resolve and encrypt a supplied credential, returning its plaintext."""
    if req.api_key is None:
        return None
    resolved_api_key, ctx.secrets_backend = await _resolve_credential_or_422(session, settings, value=req.api_key)
    updates["credentials_ciphertext"] = _encrypt(  # nosemgrep: credential-not-in-ctx
        resolved_api_key, settings.fernet_key
    )
    ctx.secret_payload["api_key"] = resolved_api_key
    return resolved_api_key


def _apply_endpoint_secret_update(
    ctx: _UpdateContext,
    endpoint: _EndpointUpdate,
    updates: dict[str, Any],
) -> None:
    """Write the resolved endpoint into the credential payload and params."""
    if not endpoint.update:
        return
    if endpoint.requested_base_url is not None:
        ctx.secret_payload["base_url"] = endpoint.requested_base_url
    else:
        ctx.secret_payload.pop("base_url", None)
    if "default_params" not in updates:
        stripped = _strip_legacy_base_url(ctx.existing.default_params)
        if stripped != dict(ctx.existing.default_params or {}):
            updates["default_params"] = stripped


async def _persist_secret_payload(
    ctx: _UpdateContext,
    endpoint: _EndpointUpdate,
    resolved_api_key: str | None,
    backend_id: uuid.UUID,
) -> None:
    """Persist the credential payload when a credential or endpoint changed."""
    if resolved_api_key is None and not endpoint.update:
        return
    stored_key = ctx.secret_payload.get("api_key")
    if not isinstance(stored_key, str) or not stored_key:
        raise _base_url_error(
            "Cannot update the endpoint without a recoverable API key; supply api_key",
            code="credentials_unavailable",
            loc="api_key",
        )
    await ctx.secrets_backend.set_secret(str(backend_id), json.dumps(ctx.secret_payload))


def _health_credential(
    resolved_api_key: str | None,
    endpoint_changed: bool,
    secret_payload: dict[str, Any],
) -> str | None:
    """Return the credential the post-commit health check must use, if any."""
    if resolved_api_key is None and not endpoint_changed:
        return None
    raw_key = resolved_api_key if resolved_api_key is not None else secret_payload.get("api_key")
    return raw_key if isinstance(raw_key, str) else None


def _build_endpoint_audit(
    current_base_url: str | None,
    effective_base_url: str | None,
    endpoint_changed: bool,
) -> dict[str, Any] | None:
    """Build the endpoint-change audit facts (hosts only, never the full URL)."""
    if not endpoint_changed:
        return None
    # Hosts only — a URL path segment can itself carry a secret.
    return {
        "from_host": _redact_base_url(current_base_url),
        "to_host": _redact_base_url(effective_base_url),
        "credential_resupplied": True,
        # A destination change only succeeds with a literal key (vault
        # references and missing keys are rejected above).
        "credential_kind": "literal",
        "allow_insecure_http": bool(effective_base_url and effective_base_url.lower().startswith("http://")),
    }


async def _update_backend_tx(
    session: AsyncSession,
    backend_id: uuid.UUID,
    principal: TenantPrincipal,
    req: ModelBackendUpdate,
    settings: Settings,
    updates: dict[str, Any],
) -> tuple[ModelBackend, ModelBackendResponse, str | None, dict[str, Any] | None]:
    """Apply the update in one transaction: RLS, fallback validation, write, secret.

    FAR-1640: a supplied ``api_key`` carrying a ``secretref://<key>`` vault
    reference is resolved and encrypted inside this transaction — the resolved
    value is never echoed.

    FAR-2046 credential binding: when the destination changes and a credential
    is already stored, the request MUST re-supply ``api_key``. Reusing a stored
    key against a new host would be a silent key-exfiltration path (the
    post-save health check sends it there). Returns the credential to use for
    the post-commit health check plus the endpoint-change audit facts.
    """
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        await set_rls_user_context(session, principal.account_id, principal.org_role)
        await _stringify_fallback_ids(session, principal.organisation_id, updates, backend_id)
        ctx = await _load_update_state(session, backend_id, principal, req, settings, updates)
        endpoint = _resolve_endpoint_update(req, ctx)
        await _recover_stored_api_key(session, backend_id, principal, settings, ctx)
        resolved_api_key = await _apply_api_key_update(session, req, settings, updates, ctx)
        _apply_endpoint_secret_update(ctx, endpoint, updates)
        await _persist_secret_payload(ctx, endpoint, resolved_api_key, backend_id)

        mb = await update_model_backend(session, backend_id, updates)
        if mb is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MODEL_BACKEND_NOT_FOUND)
        await session.refresh(mb)
        effective_base_url = _effective_base_url(ctx.secret_payload, mb.default_params, mb.provider)
        response = _to_response(mb, base_url=effective_base_url, can_manage=True)
        health_api_key = _health_credential(resolved_api_key, endpoint.changed, ctx.secret_payload)
        endpoint_audit = _build_endpoint_audit(ctx.current_base_url, effective_base_url, endpoint.changed)
        return mb, response, health_api_key, endpoint_audit


async def _update_and_post_process(
    session: AsyncSession,
    backend_id: uuid.UUID,
    principal: TenantPrincipal,
    req: ModelBackendUpdate,
    settings: Settings,
    updates: dict[str, Any],
) -> ModelBackendResponse:
    """Apply the update, then run the post-commit health check and audit appends.

    The entity write COMMITS inside ``_update_backend_tx``. A credential change
    re-runs the PRD 8.1 health check OUTSIDE the write transaction (post-rotation
    validation) so the provider network call never holds the DB connection
    or row lock; the result is persisted in a short second transaction.

    PRD §8.12 audit trail: backend edits and credential rotation were
    previously invisible. Written in fresh transactions (the update above
    already committed) and failure-isolated so a broken append never fails
    a completed update. ``model_backend_credentials_updated`` fires under
    its exact PRD name when an API key is supplied; the generic edit event
    carries only the non-credential fields that actually changed.
    """
    mb, response, health_api_key, endpoint_audit = await _update_backend_tx(
        session, backend_id, principal, req, settings, updates
    )
    if health_api_key is not None:
        await _run_health_check_on_save_and_persist(
            session,
            mb,
            mb.provider,
            mb.model_id,
            health_api_key,
            dict(mb.default_params or {}),
            org_id=principal.organisation_id,
            user_id=principal.account_id,
            org_role=principal.org_role,
            base_url=response.base_url,
        )
    changed_fields = _audit_safe_backend_fields(updates)
    if endpoint_audit is not None:
        # Endpoint changes are audited by host only, never by full URL value.
        changed_fields["base_url"] = endpoint_audit
    if changed_fields:
        await append_audit_event_isolated(
            session,
            principal,
            resource_type="model_backend",
            event_type="model_backend.updated",
            resource_id=mb.id,
            payload={"backend_id": str(mb.id), "changed_fields": changed_fields},
            log_key=_CODE_MODEL_BACKENDS_AUDIT_APPEND_FAILED,
        )
    if req.api_key is not None:
        await append_audit_event_isolated(
            session,
            principal,
            resource_type="model_backend",
            event_type="model_backend_credentials_updated",
            resource_id=mb.id,
            payload={
                "backend_id": str(mb.id),
                "name": mb.name,
                "provider": mb.provider,
                "model_id": mb.model_id,
                "credential_kind": (
                    "vault_reference" if req.api_key is not None and _is_vault_reference(req.api_key) else "literal"
                ),
            },
            log_key=_CODE_MODEL_BACKENDS_AUDIT_APPEND_FAILED,
        )
    return response


async def _apply_backend_update(
    session: AsyncSession,
    backend_id: uuid.UUID,
    principal: TenantPrincipal,
    req: ModelBackendUpdate,
    settings: Settings,
    updates: dict[str, Any],
) -> ModelBackendResponse:
    """Apply the update, mapping DB errors to the route's HTTP error contract."""
    try:
        return await _update_and_post_process(session, backend_id, principal, req, settings, updates)
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_UPDATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_UPDATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "model_backends._apply_backend_update")
        logger.exception(_CODE_MODEL_BACKENDS_UPDATE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error updating model backend")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while updating model backend.",
        ) from None


@router.patch(
    "/{backend_id}",
    dependencies=[
        Depends(deny_break_glass_mint_any_credential),
        Depends(audited("model_backend_updated", "model_backend", principal_dep=get_current_tenant_user_or_api_key)),
    ],
)
@handle_db_errors(_CODE_MODEL_BACKENDS_UPDATE_MODEL)
async def update_model_backend_endpoint(
    backend_id: uuid.UUID,
    req: ModelBackendUpdate,
    session: AsyncSession = Depends(get_db_session),
    # any_credential: declarative apply (FAR-681) updates backends with mk_ keys.
    principal: TenantPrincipal = require_permission_any_credential("model_backend.update"),
    settings: Settings = Depends(get_settings),
) -> ModelBackendResponse:
    updates = _prepare_update_payload(req)
    return await _apply_backend_update(session, backend_id, principal, req, settings, updates)


@router.post(
    "/{backend_id}/health-check",
    dependencies=[
        Depends(
            audited("model_backend_health_checked", "model_backend", principal_dep=get_current_tenant_user_or_api_key)
        )
    ],
)
@handle_db_errors(_CODE_MODEL_BACKENDS_RECHECK_MODEL)
async def recheck_model_backend_health_endpoint(
    backend_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    # any_credential: declarative apply (FAR-681) re-checks health after each
    # backend create/update with mk_ keys, so unhealthy credentials surface.
    principal: TenantPrincipal = require_permission_any_credential("model_backend.update"),
    settings: Settings = Depends(get_settings),
) -> ModelBackendHealthCheckResponse:
    """Re-run the health check on demand and persist the result (PRD §8.1).

    Operators use this to re-validate a backend after a transient save-time
    outage — the only alternative before this route was PATCHing a new API key,
    which made a sticky ``last_health_check_error`` un-clearable without rotation.
    The stored credential is decrypted and re-pinged against the provider; the
    result is persisted in a short transaction after the read transaction
    commits, so the network call never holds a DB connection or row lock.
    """
    base_url: str | None = None
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            mb = await get_model_backend(session, backend_id)
            if mb is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model backend not found")
            secrets_backend = create_secrets_backend(fernet_key=settings.fernet_key, session=session)
            payload = await _read_secret_payload(secrets_backend, backend_id)
            base_url = _effective_base_url(payload, mb.default_params, mb.provider)
            try:
                api_key = await decode_stored_secret_scoped(
                    session, mb.credentials_ciphertext, settings.fernet_key, org_id=principal.organisation_id
                )
            except Exception:
                logger.warning("Failed to decrypt credentials for model backend %s; health check skipped", backend_id)
                api_key = None
        status_, detail = await _run_health_check_on_save_and_persist(
            session,
            mb,
            mb.provider,
            mb.model_id,
            api_key,
            dict(mb.default_params or {}),
            org_id=principal.organisation_id,
            user_id=principal.account_id,
            org_role=principal.org_role,
            base_url=base_url,
        )
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_RECHECK_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_RECHECK_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Model backends are not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "model_backends.recheck_model_backend_health_endpoint")
        logger.exception(_CODE_MODEL_BACKENDS_RECHECK_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while re-checking model backend health.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error re-checking model backend health")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while re-checking model backend health.",
        ) from None
    label = {"ok": "healthy", "unhealthy": "unhealthy", "not_applicable": "not_applicable"}[status_]
    return ModelBackendHealthCheckResponse(status=label, detail=detail, checked_at=datetime.now(UTC))


@router.delete(
    "/{backend_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(deny_break_glass_mint),
        Depends(
            audited("model_backend_deleted", "model_backend", principal_dep=get_current_tenant_user, fail_closed=True)
        ),
    ],
)
@handle_db_errors(_CODE_MODEL_BACKENDS_DELETE_MODEL)
async def delete_model_backend_endpoint(
    backend_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("model_backend.delete"),
) -> None:
    audit_payload: dict[str, Any] | None = None
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            referencing = await list_backends_referencing_fallback(
                session, org_id=principal.organisation_id, backend_id=backend_id
            )
            if referencing:
                names = ", ".join(sorted(mb.name for mb in referencing))
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(f"Cannot delete model backend: it is referenced as a fallback by backend(s): {names}"),
                )
            # PRD §8.1 deletion protection: a backend pinned by a PipelineSnapshot
            # associated with a non-terminal run cannot be hard-deleted — the
            # in-flight run may still resolve the pinned backend (e.g. after a
            # HITL pause/resume). Terminal-run snapshots impose no constraint.
            non_terminal = await _list_snapshots_referencing_backend(
                session, org_id=principal.organisation_id, backend_id=backend_id
            )
            if non_terminal:
                snapshot_ids = ", ".join(sorted(str(r["snapshot_id"]) for r in non_terminal))
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Cannot delete model backend: it is pinned by a PipelineSnapshot associated with a "
                        f"non-terminal run (snapshot(s): {snapshot_ids}). Wait for the runs to finish before "
                        "hard-deleting the backend."
                    ),
                )
            # Capture the entity details BEFORE the delete so the audit event can
            # survive the row (a post-delete read would return nothing).
            existing = await get_model_backend(session, backend_id)
            if existing is None or existing.organisation_id != principal.organisation_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_NOT_FOUND)
            if existing is not None:
                audit_payload = {
                    "name": existing.name,
                    "provider": existing.provider,
                    "model_id": existing.model_id,
                    "tier": existing.tier,
                }
            deleted = await delete_model_backend(session, backend_id)
    except IntegrityError:
        logger.exception(_CODE_MODEL_BACKENDS_DELETE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        logger.exception(_CODE_MODEL_BACKENDS_DELETE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_MODEL_BACKENDS_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "model_backends.delete_model_backend_endpoint")
        logger.exception(_CODE_MODEL_BACKENDS_DELETE_MODEL)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while deleting model backend.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error deleting model backend")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while deleting model backend.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_MODEL_BACKEND_NOT_FOUND)

    # PRD §8.12 audit trail: backend deletion was previously invisible. Written
    # in a fresh transaction (the delete above already committed) and
    # failure-isolated so a broken append never fails a completed delete.
    if audit_payload is not None:
        await append_audit_event_isolated(
            session,
            principal,
            resource_type="model_backend",
            event_type="model_backend.deleted",
            resource_id=backend_id,
            payload=audit_payload,
            log_key=_CODE_MODEL_BACKENDS_AUDIT_APPEND_FAILED,
        )
