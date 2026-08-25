"""PagerDutyConnector — async PagerDuty REST API v2 connector."""

import asyncio
import contextlib
from typing import Any, cast

import httpx

from modulo._types import _DICT_STR_ANY
from modulo.connectors._safe_int import safe_int as _safe_int
from modulo.connectors._safe_page import safe_paging_total as _safe_paging_total
from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_BASE = "https://api.pagerduty.com"


def _body_field(body: object, key: str, default: object) -> object:
    """Return ``body[key]``, or *default* for a non-dict body.

    List endpoints read cursor/offset fields straight off the response body,
    so a corrupt or hostile non-dict body (list, string, number, ...) must
    not crash the parse with ``AttributeError`` on ``body.get()``.
    """
    if not isinstance(body, dict):
        return default
    return body.get(key, default)


def _next_offset_cursor(offset: object, records: list[Any], more: object) -> str | None:
    """Derive the next-page cursor from the current offset and page size.

    Emits a cursor only when PagerDuty's ``more`` flag is truthy (the API
    omits it — or sets ``false`` — on the final page). The offset is coerced
    via the shared ``safe_int`` so a corrupt value (non-finite float, bool, or
    garbage) can neither crash the parse nor poison the next request. A
    non-dict ``more`` value — possible in a corrupt response — is treated as
    absent so it cannot trigger an unbounded pagination loop.
    """
    if not more:
        return None
    return str(_safe_int(offset) + len(records))


class PagerDutyConnector(ConnectorBase):
    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.PAGERDUTY

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_BASE,
            headers={
                "Authorization": f"Token token={self._token}",
                "Accept": "application/vnd.pagerduty+json;version=2",
            },
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get("/users", params={"limit": 1})
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="PagerDuty API token validated")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid PagerDuty API token")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "incidents":
                    return await self._list_incidents(c, q)
                case "services":
                    return await self._list_services(c, q)
                case "teams":
                    return await self._list_teams(c, q)
                case "users":
                    return await self._list_users(c, q)
                case "escalation_policies":
                    return await self._list_escalation_policies(c, q)
                case "schedules":
                    return await self._list_schedules(c, q)
                case "on_calls":
                    return await self._list_on_calls(c, q)
                case _:
                    raise ValueError(f"Unsupported PagerDuty resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "incident":
                    return await self._trigger_incident(c, payload.data)
                case "incident_acknowledge":
                    return await self._acknowledge_incident(c, payload.data)
                case "incident_resolve":
                    return await self._resolve_incident(c, payload.data)
                case "note":
                    return await self._add_note(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported PagerDuty write resource: {payload.resource!r}")

    async def _list_incidents(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        for key in ("statuses", "urgency", "sort_by", "team_ids", "service_ids", "since", "until"):
            if key in q.filters:
                params[key] = q.filters[key]
        if q.limit:
            params["limit"] = q.limit
        offset: int = 0
        if q.cursor:
            with contextlib.suppress(ValueError):
                offset = int(q.cursor)
            params["offset"] = offset
        resp = await c.get("/incidents", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "incidents")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_safe_paging_total(body, "total"),
            next_cursor=_next_offset_cursor(offset, records, _body_field(body, "more", None)),
        )

    async def _list_services(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.filters.get("query"):
            params["query"] = q.filters["query"]
        if q.filters.get("sort_by"):
            params["sort_by"] = q.filters["sort_by"]
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["offset"] = q.cursor
        resp = await c.get("/services", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "services")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_safe_paging_total(body, "total"),
            next_cursor=_next_offset_cursor(_body_field(body, "offset", 0), records, _body_field(body, "more", None)),
        )

    async def _list_teams(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.filters.get("query"):
            params["query"] = q.filters["query"]
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["offset"] = q.cursor
        resp = await c.get("/teams", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "teams")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_safe_paging_total(body, "total"),
            next_cursor=_next_offset_cursor(_body_field(body, "offset", 0), records, _body_field(body, "more", None)),
        )

    async def _list_users(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.filters.get("query"):
            params["query"] = q.filters["query"]
        if q.filters.get("team_ids"):
            params["team_ids"] = q.filters["team_ids"]
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["offset"] = q.cursor
        resp = await c.get("/users", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "users")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_safe_paging_total(body, "total"),
            next_cursor=_next_offset_cursor(_body_field(body, "offset", 0), records, _body_field(body, "more", None)),
        )

    async def _list_escalation_policies(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.filters.get("query"):
            params["query"] = q.filters["query"]
        if q.filters.get("sort_by"):
            params["sort_by"] = q.filters["sort_by"]
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["offset"] = q.cursor
        resp = await c.get("/escalation_policies", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "escalation_policies")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_safe_paging_total(body, "total"),
            next_cursor=_next_offset_cursor(_body_field(body, "offset", 0), records, _body_field(body, "more", None)),
        )

    async def _list_schedules(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.filters.get("query"):
            params["query"] = q.filters["query"]
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["offset"] = q.cursor
        resp = await c.get("/schedules", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "schedules")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_safe_paging_total(body, "total"),
            next_cursor=_next_offset_cursor(_body_field(body, "offset", 0), records, _body_field(body, "more", None)),
        )

    async def _list_on_calls(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        for key in ("team_ids", "schedule_ids", "user_ids", "since", "until", "earliest"):
            if key in q.filters:
                params[key] = q.filters[key]
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["offset"] = q.cursor
        resp = await c.get("/oncalls", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "oncalls")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_safe_paging_total(body, "total"),
            next_cursor=_next_offset_cursor(_body_field(body, "offset", 0), records, _body_field(body, "more", None)),
        )

    async def _trigger_incident(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        title = data.get("title")
        service_id = data.get("service_id")
        if not title or not service_id:
            raise ValueError("PagerDuty incident write requires 'title' and 'service_id' in data")
        incident: dict[str, Any] = {
            "type": "incident",
            "title": title,
            "service": {"id": service_id, "type": "service_reference"},
        }
        if data.get("urgency"):
            incident["urgency"] = data["urgency"]
        if data.get("body"):
            incident["body"] = {"type": "incident_body", "details": data["body"]}
        if data.get("escalation_policy_id"):
            incident["escalation_policy"] = {
                "id": data["escalation_policy_id"],
                "type": "escalation_policy_reference",
            }
        if data.get("priority_id"):
            incident["priority"] = {"id": data["priority_id"], "type": "priority_reference"}
        body: dict[str, Any] = {"incident": incident}
        resp = await c.post("/incidents", json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("incident", {}))

    async def _acknowledge_incident(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        incident_id = data.get("incident_id")
        if not incident_id:
            raise ValueError("PagerDuty incident_acknowledge write requires 'incident_id' in data")
        body: dict[str, Any] = {
            "incident": {
                "type": "incident",
                "status": "acknowledged",
            },
        }
        resp = await c.put(f"/incidents/{incident_id}", json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("incident", {}))

    async def _resolve_incident(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        incident_id = data.get("incident_id")
        if not incident_id:
            raise ValueError("PagerDuty incident_resolve write requires 'incident_id' in data")
        body: dict[str, Any] = {
            "incident": {
                "type": "incident",
                "status": "resolved",
            },
        }
        resp = await c.put(f"/incidents/{incident_id}", json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("incident", {}))

    async def _add_note(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        incident_id = data.get("incident_id")
        content = data.get("content")
        if not incident_id or not content:
            raise ValueError("PagerDuty note write requires 'incident_id' and 'content' in data")
        body: dict[str, Any] = {"note": {"content": content}}
        resp = await c.post(f"/incidents/{incident_id}/notes", json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("note", {}))
