"""PlaneConnector — read/write Plane work items on a self-hosted instance.

Plane ships a self-hosted Community edition and a dedicated air-gapped
edition, making it squarely the sovereign issue tracker for the self-hosted
ICP (FAR-1767). This connector targets the documented stable **v1** API
(``/api/v1/``); Plane Cloud uses the same shape at ``https://api.plane.so``.

**API shape** — verified against the self-hosted source (``server``-side URL
conf in ``apps/api/plane/api/urls/work_item.py`` / ``urls/project.py`` and the
``IssueListCreateAPIEndpoint`` / ``IssueSerializer`` views), not from a summary:

* ``GET/POST /workspaces/{slug}/projects/`` and ``GET/PATCH
  /workspaces/{slug}/projects/{project_id}/`` — projects.
* ``GET/POST /workspaces/{slug}/projects/{project_id}/work-items/`` and
  ``GET/PATCH /workspaces/{slug}/projects/{project_id}/work-items/{id}/`` —
  work items (list / create / retrieve / update).
* ``GET /workspaces/{slug}/work-items/{PROJ-123}/`` — retrieve by the
  human-readable identifier.
* ``GET /workspaces/{slug}/work-items/search/?q=`` — workspace search.

Spec deviations from FAR-1767's prose, verified: there is **no** bare
workspace-wide ``GET /workspaces/{slug}/work-items/`` list route in the
API-key surface (only search + by-identifier live at that level), and the
work-item write/serialiser fields are the model's own (``name``,
``description_html``, ``state``, ``assignees``, ``labels``, ``parent``, ...)
rather than the ``state_id`` / ``assignee_ids`` spellings the ticket listed —
so payloads and records pass through **verbatim**: callers use Plane's own
field names and receive Plane's own objects, with no renames that could drift
as the API evolves.

**Auth:** personal access token in the ``X-API-Key`` header. **Errors:** 401
missing/invalid key, 403 scope/role, 404 not found/out of tenant, 429
rate-limited (``Retry-After`` honoured, then a bounded number of retries);
each surfaces as a credential-redacted ``ValueError`` with the status-specific
hint. **Pagination:** cursor envelopes (``next_cursor`` / ``prev_cursor`` /
``count`` / ``total_results`` / ``results``) via ``per_page`` + ``cursor``.

Only the list parameters the API documents are ever forwarded (``order_by``,
``expand``, ``fields``, ``per_page``, ``cursor``): the self-hosted edition
rejects the generic ``pql``/``filters`` params with a 400, and a filter that
was silently dropped would be worse — an unknown filter fails loud here.

Integration against a real Plane container is deferred to CI (Docker is
unavailable on the implementing workstation); the unit tests mock the API.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, cast

import httpx

from modulo._types import _DICT_STR_ANY
from modulo.connectors._egress import egress_ssl_context
from modulo.connectors._retry_headers import RETRYABLE_STATUSES
from modulo.connectors._retry_headers import backoff_delay as _backoff_delay
from modulo.connectors._retry_headers import retry_after_seconds as _retry_after_seconds
from modulo.connectors._safe_cursor import safe_cursor as _safe_cursor
from modulo.connectors._safe_int import safe_int as _safe_int
from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.core.ssrf import pinned_async_client_sync

logger = logging.getLogger(__name__)

#: Retries for transient statuses (429/502/503/504) before failing loud.
_MAX_RETRIES = 3

#: The list/query parameters Plane's list endpoints accept (verified against
#: the API-key view's documented parameters). Anything else in a query's
#: ``filters`` raises rather than being silently dropped.
_ALLOWED_LIST_PARAMS = frozenset({"order_by", "expand", "fields", "per_page", "cursor"})

#: Filters this connector CONSUMES as path selectors (they address the
#: resource, they are never forwarded as query parameters).
_ROUTING_FILTERS = frozenset({"project_id", "work_item_id", "identifier"})


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    """The delay before retry *attempt*: the upstream ``Retry-After`` (clamped) or backoff."""
    parsed = _retry_after_seconds(response) if response is not None else None
    if parsed is not None:
        return parsed
    return _backoff_delay(attempt)


class PlaneConnector(ConnectorBase):
    """Read/write Plane work items, projects and search (self-hosted Plane).

    Credentials (from credentials_ciphertext):
      "token"  — Plane personal access token (sent as ``X-API-Key``)

    Config (config_json):
      "base_url"        — REQUIRED, the API root (``https://<host>/api/v1``)
      "workspace_slug"  — REQUIRED, the workspace this connector operates in

    Supported query resources:
      "projects"    — list the workspace's projects (cursor paginated)
      "project"     — get one project; requires "project_id" filter
      "work_items"  — list a project's work items; requires "project_id";
                      optional "order_by"/"expand"/"fields"/"per_page"/"cursor"
      "work_item"   — get one work item; requires "work_item_id"+"project_id",
                      or "identifier" (e.g. PROJ-123) for a workspace-wide
                      identifier lookup
      "search"      — workspace work-item search; requires "q" filter

    Supported write resources:
      "work_item"         — create; data: {"project_id": ..., ...plane fields}
      "work_item_update"  — update; data: {"project_id": ...,
                            "work_item_id": ..., ...plane fields}

    Payload fields pass through verbatim (Plane's own field names); records
    are returned as Plane returned them.
    """

    def __init__(self, token: str, base_url: str, workspace_slug: str) -> None:
        self._token = token
        self._base_url = base_url.strip().rstrip("/")
        if not self._base_url:
            raise ValueError("base_url is required for the Plane connector")
        self._workspace_slug = workspace_slug.strip()
        if not self._workspace_slug or "/" in self._workspace_slug or self._workspace_slug in {".", ".."}:
            raise ValueError(
                f"workspace_slug must be a single non-empty path segment, got {workspace_slug!r}",
            )

    def _credential_values(self) -> Sequence[str]:
        return (self._token,)

    @property
    def connector_type(self) -> ConnectorType:
        # "plane" is a hub-native connector-type id (deliberately not a
        # ConnectorType enum member). The object's type is the issue-tracker
        # family member: Plane is work-item tracking, and TICKET_TRACKER carries
        # the ticket read/write/search capability set.
        return ConnectorType.TICKET_TRACKER

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self._token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        # PINNED TRANSPORT (FAR-520): the base_url comes from tenant connector
        # config (self-hosted / air-gapped Plane), so it is on the external
        # trust boundary — validate + resolve the host synchronously and pin
        # the validated IP onto the transport (closes the DNS-rebinding window).
        return pinned_async_client_sync(
            self._base_url,
            base_url=self._base_url,
            headers=self._headers(),
            timeout=30,
            verify=egress_ssl_context(self),
        )

    # ------------------------------------------------------------------
    # Shared request path
    # ------------------------------------------------------------------

    def _ws_path(self, *segments: str) -> str:
        """A workspace-scoped API path (always trailing-slashed, per Plane's routes)."""
        parts = ["workspaces", self._workspace_slug, *segments]
        return "/".join(parts) + "/"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """One retried Plane API request, returning the decoded JSON body.

        Transient statuses (429/502/503/504 — ``RETRYABLE_STATUSES``) are
        retried with the upstream ``Retry-After`` honoured (clamped) or an
        exponential backoff; everything else fails loud as a credential-redacted
        ``ValueError`` carrying the status-specific hint, and transport failures
        (``httpx.HTTPError``) are normalised to the same contract. A success
        with a non-JSON body also fails loud (never a silent empty result).
        """
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._client() as client:
                    response = await client.request(method, path, params=params, json=json)
            except httpx.HTTPError as exc:
                # A transport failure (connect/timeout/…) is normalised to the
                # same credential-redacted ValueError contract as every other
                # connector error, so a caller never sees a raw httpx type.
                raise ValueError(self._redacted_detail(f"Plane API HTTP error for {path}: {exc}")) from exc
            if response.status_code in RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                delay = _retry_delay(attempt, response)
                logger.warning(
                    "Plane API %s %s returned HTTP %s — retrying in %.1fs (attempt %d/%d)",
                    method,
                    path,
                    response.status_code,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ValueError(self._error_message(exc)) from exc
            try:
                return response.json()
            except ValueError as exc:
                raise ValueError(f"Plane API returned a non-JSON response for {path}") from exc
        # Unreachable in practice (the final attempt always returns above); a
        # loud guard so the loop can never fall off its end silently.
        raise ValueError(f"Plane API request for {path!r} exhausted its retry budget")

    def _error_message(self, exc: httpx.HTTPStatusError) -> str:
        """A credential-redacted, status-specific error for one failed response."""
        status = exc.response.status_code
        text = self._redacted_detail(exc.response.text)[:200]
        hints = {
            401: "authentication failed — the X-API-Key token is missing or invalid",
            403: "forbidden — the token's scope or role does not allow this operation",
            404: "not found — wrong id, or the workspace/resource is outside this instance",
            429: "rate limited — Retry-After was honoured and the retries were exhausted",
        }
        hint = hints.get(status)
        if hint is not None:
            return f"Plane API error ({status}): {hint} — {text}"
        return f"Plane API error ({status}): {text}"

    @staticmethod
    def _require_filter(q: ConnectorQuery, key: str) -> str:
        value = q.filters.get(key)
        if value is None or value == "":
            raise ValueError(f"Plane {q.resource!r} query requires a {key!r} filter")
        return str(value)

    @staticmethod
    def _require_data(payload: ConnectorPayload, key: str) -> str:
        value = payload.data.get(key)
        if value is None or value == "":
            raise ValueError(f"Plane {payload.resource!r} write requires {key!r} in data")
        return str(value)

    @staticmethod
    def _page_result(body: Any) -> ConnectorResult:
        """Build a :class:`ConnectorResult` from a list or cursor-envelope body."""
        if isinstance(body, list):
            records = [record for record in body if isinstance(record, dict)]
            return ConnectorResult(records=records, total=len(records))
        if isinstance(body, dict):
            raw_total = body.get("total_results")
            total = _safe_int(raw_total) if raw_total is not None else None
            return ConnectorResult(
                records=_safe_records(body, "results"),
                next_cursor=_safe_cursor(body.get("next_cursor")),
                total=total,
            )
        return ConnectorResult(records=[])

    def _list_params(self, q: ConnectorQuery) -> dict[str, Any]:
        """The verified list parameters for *q*, failing loud on anything else."""
        params: dict[str, Any] = {}
        if q.limit:
            params["per_page"] = q.limit
        if q.cursor:
            params["cursor"] = q.cursor
        for key, value in q.filters.items():
            if key in _ROUTING_FILTERS:
                # Selectors the connector already consumed to build the path —
                # not query parameters, so they are not forwarded (and not
                # silently dropped: they had their effect on the URL).
                continue
            if key not in _ALLOWED_LIST_PARAMS:
                raise ValueError(
                    f"Unsupported Plane filter {key!r} for {q.resource!r} (supported: {sorted(_ALLOWED_LIST_PARAMS)})",
                )
            params[key] = value
        return params

    # ------------------------------------------------------------------
    # ConnectorBase contract
    # ------------------------------------------------------------------

    async def health_check(self) -> HealthResult:
        """Probe the workspace's project list — connectivity AND token validity."""
        try:
            await self._request("GET", self._ws_path("projects"), params={"per_page": 1})
        except ValueError as exc:
            # Includes the outbound SSRF guard's ValueError (a blocked
            # base_url reports unhealthy with its remediation text, never a
            # 502-raising exception).
            return HealthResult(ok=False, detail=self._redacted_detail(str(exc))[:200])
        except httpx.HTTPError as exc:
            return HealthResult(ok=False, detail=self._redacted_detail(f"HTTP error: {exc}")[:200])
        return HealthResult(ok=True)

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        match q.resource:
            case "projects":
                return self._page_result(
                    await self._request("GET", self._ws_path("projects"), params=self._list_params(q))
                )
            case "project":
                project_id = self._require_filter(q, "project_id")
                body = await self._request("GET", self._ws_path("projects", project_id))
                return ConnectorResult(records=[body] if isinstance(body, dict) else [])
            case "work_items":
                project_id = self._require_filter(q, "project_id")
                body = await self._request(
                    "GET",
                    self._ws_path("projects", project_id, "work-items"),
                    params=self._list_params(q),
                )
                return self._page_result(body)
            case "work_item":
                return await self._query_work_item(q)
            case "search":
                term = self._require_filter(q, "q")
                body = await self._request("GET", self._ws_path("work-items", "search"), params={"q": term})
                return self._page_result(body)
            case _:
                raise ValueError(f"Unsupported Plane query resource: {q.resource!r}")

    async def _query_work_item(self, q: ConnectorQuery) -> ConnectorResult:
        """One work item, by ``identifier`` (PROJ-123) or by ids."""
        identifier = q.filters.get("identifier")
        if identifier:
            body = await self._request("GET", self._ws_path("work-items", str(identifier)))
            return ConnectorResult(records=[body] if isinstance(body, dict) else [])
        project_id = self._require_filter(q, "project_id")
        work_item_id = self._require_filter(q, "work_item_id")
        body = await self._request(
            "GET",
            self._ws_path("projects", project_id, "work-items", work_item_id),
        )
        return ConnectorResult(records=[body] if isinstance(body, dict) else [])

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        match payload.resource:
            case "work_item":
                project_id = self._require_data(payload, "project_id")
                data = {key: value for key, value in payload.data.items() if key != "project_id"}
                body = await self._request(
                    "POST",
                    self._ws_path("projects", project_id, "work-items"),
                    json=data,
                )
                if not isinstance(body, dict):
                    raise ValueError("Plane API returned a non-object body creating a work item")
                return cast(_DICT_STR_ANY, body)

            case "work_item_update":
                project_id = self._require_data(payload, "project_id")
                work_item_id = self._require_data(payload, "work_item_id")
                data = {key: value for key, value in payload.data.items() if key not in {"project_id", "work_item_id"}}
                body = await self._request(
                    "PATCH",
                    self._ws_path("projects", project_id, "work-items", work_item_id),
                    json=data,
                )
                if not isinstance(body, dict):
                    raise ValueError("Plane API returned a non-object body updating a work item")
                return cast(_DICT_STR_ANY, body)

            case _:
                raise ValueError(f"Unsupported Plane write resource: {payload.resource!r}")
