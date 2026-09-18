"""N8NConnector — async n8n REST API connector."""

import asyncio
from typing import Any, cast

import httpx

from modulo._types import _DICT_STR_ANY
from modulo.connectors._safe_cursor import safe_cursor as _safe_cursor
from modulo.connectors._safe_page import safe_records as _safe_records
from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
    health_check_failure,
)
from modulo.connectors.security import CredentialRedactor, redacting
from modulo.core.ssrf import pinned_async_client_sync

# Repeated REST path (S1192).
_WORKFLOWS_PATH = "/rest/workflows"


class N8NConnector(ConnectorBase):
    def __init__(self, token: str, base_url: str = "http://localhost:5678") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._redactor = CredentialRedactor([token])

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.N8N

    def _client(self) -> httpx.AsyncClient:
        # PINNED TRANSPORT (FAR-512): validate + resolve the base_url's host and
        # pin the validated IP onto the transport so the connection never
        # re-resolves at connect time (closes DNS-rebind). ``trust_env=False``
        # stops a proxy from re-resolving the destination and defeating the pin.
        return pinned_async_client_sync(
            self._base_url,
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            timeout=30,
        )

    async def health_check(self) -> HealthResult:
        try:
            async with self._client() as c:
                resp = await c.get(_WORKFLOWS_PATH, params={"limit": 1})
                if resp.status_code == 200:
                    return HealthResult(ok=True, detail="n8n API is reachable and token is valid")
                if resp.status_code == 401:
                    return HealthResult(ok=False, detail="Invalid n8n API token")
                return HealthResult(
                    ok=False, detail=self._redactor.redact(f"HTTP {resp.status_code}: {resp.text[:200]}")
                )
        except httpx.ConnectError as exc:
            return HealthResult(ok=False, detail=self._redactor.redact(f"Cannot connect to n8n: {exc}"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return health_check_failure(self._redactor.redact_exc(exc))

    @redacting
    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        async with self._client() as c:
            match q.resource:
                case "workflows":
                    return await self._list_workflows(c, q)
                case "workflow":
                    return await self._get_workflow(c, q)
                case "executions":
                    return await self._list_executions(c, q)
                case "execution":
                    return await self._get_execution(c, q)
                case "webhooks":
                    return await self._list_webhooks(c, q)
                case "credentials":
                    return await self._list_credentials(c, q)
                case "credential":
                    return await self._get_credential(c, q)
                case "tags":
                    return await self._list_tags(c, q)
                case "nodes":
                    return await self._list_nodes(c, q)
                case _:
                    raise ValueError(f"Unsupported n8n resource: {q.resource!r}")

    @redacting
    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        async with self._client() as c:
            match payload.resource:
                case "workflow":
                    return await self._create_workflow(c, payload.data)
                case "workflow_update":
                    return await self._update_workflow(c, payload.data)
                case "workflow_activate":
                    return await self._activate_workflow(c, payload.data)
                case "workflow_deactivate":
                    return await self._deactivate_workflow(c, payload.data)
                case "workflow_delete":
                    return await self._delete_workflow(c, payload.data)
                case "execution_delete":
                    return await self._delete_execution(c, payload.data)
                case "credential":
                    return await self._create_credential(c, payload.data)
                case "execution_retry":
                    return await self._retry_execution(c, payload.data)
                case _:
                    raise ValueError(f"Unsupported n8n write resource: {payload.resource!r}")

    async def _list_workflows(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        for key in ("active", "tags"):
            if key in q.filters:
                params[key] = q.filters[key]
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get(_WORKFLOWS_PATH, params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
            next_cursor=_safe_cursor(body.get("nextCursor") if isinstance(body, dict) else None),
        )

    async def _get_workflow(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        workflow_id = q.filters.get("id") if q.filters else None
        if not workflow_id:
            raise ValueError("n8n workflow query requires 'id' filter")
        resp = await c.get(f"/rest/workflows/{workflow_id}")
        resp.raise_for_status()
        body = resp.json()
        record = body.get("data", {}) if isinstance(body, dict) else {}
        return ConnectorResult(records=[record])

    async def _list_executions(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        for key in ("status", "workflowId"):
            if key in q.filters:
                params[key] = q.filters[key]
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get("/rest/executions", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
            next_cursor=_safe_cursor(body.get("nextCursor") if isinstance(body, dict) else None),
        )

    async def _get_execution(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        execution_id = q.filters.get("id") if q.filters else None
        if not execution_id:
            raise ValueError("n8n execution query requires 'id' filter")
        resp = await c.get(f"/rest/executions/{execution_id}")
        resp.raise_for_status()
        body = resp.json()
        record = body.get("data", {}) if isinstance(body, dict) else {}
        return ConnectorResult(records=[record])

    async def _list_webhooks(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get("/rest/webhooks", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
            next_cursor=_safe_cursor(body.get("nextCursor") if isinstance(body, dict) else None),
        )

    async def _list_credentials(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get("/rest/credentials", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
            next_cursor=_safe_cursor(body.get("nextCursor") if isinstance(body, dict) else None),
        )

    async def _get_credential(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        credential_id = q.filters.get("id") if q.filters else None
        if not credential_id:
            raise ValueError("n8n credential query requires 'id' filter")
        resp = await c.get(f"/rest/credentials/{credential_id}")
        resp.raise_for_status()
        body = resp.json()
        record = body.get("data", {}) if isinstance(body, dict) else {}
        return ConnectorResult(records=[record])

    async def _list_tags(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get("/rest/tags", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
            next_cursor=_safe_cursor(body.get("nextCursor") if isinstance(body, dict) else None),
        )

    async def _list_nodes(self, c: httpx.AsyncClient, q: ConnectorQuery) -> ConnectorResult:
        params: dict[str, Any] = {}
        if q.limit:
            params["limit"] = q.limit
        if q.cursor:
            params["cursor"] = q.cursor
        resp = await c.get("/rest/node-types", params=params)
        resp.raise_for_status()
        body = resp.json()
        records = _safe_records(body, "data")
        return ConnectorResult(
            records=records[: q.limit or len(records)],
            total=len(records),
            next_cursor=_safe_cursor(body.get("nextCursor") if isinstance(body, dict) else None),
        )

    async def _create_workflow(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        name = data.get("name")
        if not name:
            raise ValueError("n8n workflow creation requires 'name' in data")
        body: dict[str, Any] = {
            "name": name,
            "nodes": data.get("nodes", []),
            "connections": data.get("connections", {}),
        }
        if "settings" in data:
            body["settings"] = data["settings"]
        if "staticData" in data:
            body["staticData"] = data["staticData"]
        if "tags" in data:
            body["tags"] = data["tags"]
        resp = await c.post(_WORKFLOWS_PATH, json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))

    async def _update_workflow(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        workflow_id = data.get("id")
        if not workflow_id:
            raise ValueError("n8n workflow update requires 'id' in data")
        body: dict[str, Any] = {}
        for key in ("name", "nodes", "connections", "settings", "staticData", "tags"):
            if key in data:
                body[key] = data[key]
        resp = await c.put(f"/rest/workflows/{workflow_id}", json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))

    async def _activate_workflow(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        workflow_id = data.get("id")
        if not workflow_id:
            raise ValueError("n8n workflow activation requires 'id' in data")
        resp = await c.post(f"/rest/workflows/{workflow_id}/activate")
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))

    async def _deactivate_workflow(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        workflow_id = data.get("id")
        if not workflow_id:
            raise ValueError("n8n workflow deactivation requires 'id' in data")
        resp = await c.post(f"/rest/workflows/{workflow_id}/deactivate")
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))

    async def _delete_workflow(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        workflow_id = data.get("id")
        if not workflow_id:
            raise ValueError("n8n workflow deletion requires 'id' in data")
        resp = await c.delete(f"/rest/workflows/{workflow_id}")
        if resp.status_code == 204:
            return {"id": workflow_id, "deleted": True}
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))

    async def _delete_execution(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        execution_id = data.get("id")
        if not execution_id:
            raise ValueError("n8n execution deletion requires 'id' in data")
        resp = await c.delete(f"/rest/executions/{execution_id}")
        if resp.status_code == 204:
            return {"id": execution_id, "deleted": True}
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))

    async def _create_credential(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        name = data.get("name")
        cred_type = data.get("type")
        if not name or not cred_type:
            raise ValueError("n8n credential creation requires 'name' and 'type' in data")
        body: dict[str, Any] = {"name": name, "type": cred_type, "data": data.get("data", {})}
        resp = await c.post("/rest/credentials", json=body)
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))

    async def _retry_execution(self, c: httpx.AsyncClient, data: dict[str, Any]) -> dict[str, Any]:
        execution_id = data.get("id")
        if not execution_id:
            raise ValueError("n8n execution retry requires 'id' in data")
        resp = await c.post(f"/rest/executions/{execution_id}/retry")
        resp.raise_for_status()
        result = cast(_DICT_STR_ANY, resp.json())
        return cast(_DICT_STR_ANY, result.get("data", {}))
