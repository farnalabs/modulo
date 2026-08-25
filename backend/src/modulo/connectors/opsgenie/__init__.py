"""OpsgenieConnector — async Opsgenie REST API v2 connector."""

import asyncio
from typing import Any, cast

import httpx

from modulo._types import _DICT_STR_ANY
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

_BASE = "https://api.opsgenie.com/v2"

# Repeated REST path (S1192).
_ALERTS_PATH = "/alerts"


def _paging_total_count(body: object) -> int | None:
    """Extract Opsgenie's ``totalCount`` as a safe int.

    Guards against non-finite floats (``inf``/``nan``) which otherwise crash
    downstream ``int()`` coercion — Python's json parser produces ``inf`` for
    overflowing literals such as ``1e999``, so a corrupt or hostile Opsgenie
    response must not be able to poison the reported total. A missing
    ``totalCount`` keeps the historical ``None`` behaviour. A non-dict body
    (list, string, number, ...) from a corrupt response is treated as absent.
    """
    if not isinstance(body, dict):
        return None
    raw = body.get("totalCount")
    if raw is None:
        return None
    return _safe_int(raw)


def _next_offset_cursor(offset: object, records: list[Any], paging: object) -> str | None:
    """Derive the next-page cursor from the current offset and page size.

    Emits a cursor only when Opsgenie's ``paging`` block is a dict that
    actually advertises a ``next`` page (the API omits ``next`` on the final
    page). A non-dict ``paging`` value — possible in a corrupt response — is
    treated as absent so it can neither crash the parse nor trigger an
    unbounded pagination loop.
    """
    if not isinstance(paging, dict) or not paging.get("next"):
        return None
    return str(_safe_int(offset) + len(records))


class OpsgenieConnector(ConnectorBase):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.OPSGENIE

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_BASE,
            headers={
                "Authorization": f"GenieKey {self._api_key}",
            },
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get(_ALERTS_PATH, params={"limit": 1})
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="Opsgenie API key validated")
                if resp.status_code in (401, 403):
                    return HealthResult(ok=False, detail="Invalid Opsgenie API key")
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "alerts":
                    return await self._query_alerts(c, q)
                case "alert":
                    return await self._query_alert(c, q)
                case "alert_notes":
                    return await self._query_alert_notes(c, q)
                case "alert_logs":
                    return await self._query_alert_logs(c, q)
                case "teams":
                    return await self._query_teams(c, q)
                case "schedules":
                    return await self._query_schedules(c, q)
                case "on_calls":
                    return await self._query_on_calls(c, q)
                case "escalations":
                    return await self._query_escalations(c, q)
                case _:
                    raise ValueError(f"Unsupported Opsgenie resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "alert":
                    return await self._create_alert(c, payload.data)
                case "alert_acknowledge":
                    return await self._acknowledge_alert(c, payload.data)
                case "alert_close":
                    return await self._close_alert(c, payload.data)
                case "alert_note":
                    return await self._add_alert_note(c, payload.data)
                case "alert_snooze":
                    return await self._snooze_alert(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported Opsgenie write resource: {payload.resource!r}")

    async def _query_alerts(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        for key in ("query", "status", "priority", "tags", "team", "limit", "offset"):
            if key in q.filters:
                params[key] = q.filters[key]
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["offset"] = q.cursor
        resp = await c.get(_ALERTS_PATH, params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_paging_total_count(body),
            next_cursor=_next_offset_cursor(
                params.get("offset", 0), records, body.get("paging") if isinstance(body, dict) else None
            ),
        )

    async def _query_alert(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        alert_id = q.filters.get("id")
        if not alert_id:
            raise ValueError("Opsgenie alert query requires 'id' in filters")
        identifier = cast("str", alert_id)
        params: dict[str, Any] = {"identifierType": "id"}
        resp = await c.get(f"/alerts/{identifier}", params=params)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", {}) if isinstance(body, dict) else {}
        return ConnectorResult(records=[data] if data else [], total=1 if data else 0)

    async def _query_alert_notes(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        alert_id = q.filters.get("id")
        if not alert_id:
            raise ValueError("Opsgenie alert_notes query requires 'id' in filters")
        identifier = cast("str", alert_id)
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["offset"] = q.cursor
        resp = await c.get(f"/alerts/{identifier}/notes", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_paging_total_count(body),
            next_cursor=str(len(records)) if records and len(records) == (q.limit or 100) else None,
        )

    async def _query_alert_logs(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        alert_id = q.filters.get("id")
        if not alert_id:
            raise ValueError("Opsgenie alert_logs query requires 'id' in filters")
        identifier = cast("str", alert_id)
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["offset"] = q.cursor
        resp = await c.get(f"/alerts/{identifier}/logs", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_paging_total_count(body),
            next_cursor=str(len(records)) if records and len(records) == (q.limit or 100) else None,
        )

    async def _query_teams(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
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
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_paging_total_count(body),
            next_cursor=str(len(records)) if records and len(records) == (q.limit or 100) else None,
        )

    async def _query_schedules(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["offset"] = q.cursor
        resp = await c.get("/schedules", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_paging_total_count(body),
            next_cursor=str(len(records)) if records and len(records) == (q.limit or 100) else None,
        )

    async def _query_on_calls(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        schedule_id = q.filters.get("schedule_id") or q.cursor
        if not schedule_id:
            raise ValueError("Opsgenie on_calls query requires 'schedule_id' in filters or cursor")
        identifier = cast("str", schedule_id)
        params: dict[str, Any] = {}
        if q.filters.get("date"):
            params["date"] = q.filters["date"]
        if q.filters.get("flat"):
            params["flat"] = q.filters["flat"]
        resp = await c.get(f"/schedules/{identifier}/on-calls", params=params)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", {}) if isinstance(body, dict) else {}
        records = [data] if data else []
        return ConnectorResult(records=records, total=len(records))

    async def _query_escalations(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["offset"] = q.cursor
        resp = await c.get("/escalations", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=_paging_total_count(body),
            next_cursor=str(len(records)) if records and len(records) == (q.limit or 100) else None,
        )

    async def _create_alert(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        message = data.get("message")
        if not message:
            raise ValueError("Opsgenie alert write requires 'message' in data")
        body: dict[str, Any] = {
            "message": message,
            "description": data.get("description", ""),
            "priority": data.get("priority", "P3"),
            "source": data.get("source", "Modulo"),
        }
        if data.get("tags"):
            body["tags"] = data["tags"]
        if data.get("entity"):
            body["entity"] = data["entity"]
        if data.get("alias"):
            body["alias"] = data["alias"]
        if data.get("responders"):
            body["responders"] = data["responders"]
        if data.get("visible_to"):
            body["visible_to"] = data["visible_to"]
        if data.get("actions"):
            body["actions"] = data["actions"]
        if data.get("note"):
            body["note"] = data["note"]
        resp = await c.post(_ALERTS_PATH, json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))

    async def _acknowledge_alert(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        alert_id = data.get("id")
        if not alert_id:
            raise ValueError("Opsgenie alert_acknowledge write requires 'id' in data")
        body: dict[str, Any] = {}
        if data.get("note"):
            body["note"] = data["note"]
        if data.get("user"):
            body["user"] = data["user"]
        if data.get("source"):
            body["source"] = data["source"]
        resp = await c.post(f"/alerts/{alert_id}/acknowledge", json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))

    async def _close_alert(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        alert_id = data.get("id")
        if not alert_id:
            raise ValueError("Opsgenie alert_close write requires 'id' in data")
        body: dict[str, Any] = {}
        if data.get("note"):
            body["note"] = data["note"]
        if data.get("source"):
            body["source"] = data["source"]
        resp = await c.post(f"/alerts/{alert_id}/close", json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))

    async def _add_alert_note(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        alert_id = data.get("id")
        note = data.get("note")
        if not alert_id or not note:
            raise ValueError("Opsgenie alert_note write requires 'id' and 'note' in data")
        body: dict[str, Any] = {"note": note}
        if data.get("user"):
            body["user"] = data["user"]
        if data.get("source"):
            body["source"] = data["source"]
        resp = await c.post(f"/alerts/{alert_id}/notes", json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))

    async def _snooze_alert(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        alert_id = data.get("id")
        end_time = data.get("end_time")
        if not alert_id or not end_time:
            raise ValueError("Opsgenie alert_snooze write requires 'id' and 'end_time' in data")
        body: dict[str, Any] = {"endTime": end_time}
        if data.get("note"):
            body["note"] = data["note"]
        if data.get("user"):
            body["user"] = data["user"]
        if data.get("source"):
            body["source"] = data["source"]
        resp = await c.post(f"/alerts/{alert_id}/snooze", json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))
