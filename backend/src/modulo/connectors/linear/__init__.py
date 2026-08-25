"""LinearConnector - thin Linear issue-tracker adapter (FAR-370).

Thin connector model: this is a contextual integration, not a deep client.

  T1 (thin reference + read)  resolve(issue_ref) -> fact dict with title,
                              status, assignee, link, updated_at.
  T2 (read-only enrichment)   get_issue_body(issue_ref) / get_comments(issue_ref).
  T3 (scoped write)           update_status(issue_ref, status) and a structured,
                              prefixed comment(issue_ref, body) (inherited base
                              method, implemented via _post_comment).

No auto-create (create_ticket) and no compensate expansion - per the thin
connector contract. Mirrors JiraConnector conventions (Bearer auth, httpx
client, typed errors, no credentials in logs) but stays deliberately narrow.

Linear's API is GraphQL at https://api.linear.app/graphql authenticated with a
Bearer API key (the connector credential ``token``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from modulo.connectors._retry_headers import RETRYABLE_STATUSES
from modulo.connectors.base import (
    ConnectorPayload,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.connectors.ticket_tracker.base import Ticket, TicketTrackerBase

_LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# Retryable transport / upstream conditions for the thin GraphQL client.
# FAR-410: the shared constant is the INTERSECTION {429, 502, 503, 504}; Linear
# historically ALSO retried 500 (Linear's GraphQL edge can 500 on a few-millisecond
# outage). That 500 clause is preserved explicitly — the shared constant is never
# silently widened, and Linear's behaviour is unchanged.
_RETRYABLE_STATUS = RETRYABLE_STATUSES | frozenset({500})
_MAX_RETRIES = 3
_BASE_DELAY = 0.5
_MAX_DELAY = 10.0


class _RetrySignalError(Exception):
    """Internal signal that a GraphQL request should be retried on the next attempt."""

    def __init__(self, exc: Exception | None) -> None:
        self.exc = exc
        super().__init__(str(exc) if exc is not None else "retry")


def _retry_delay(attempt: int) -> float:
    """Exponential backoff capped at ``_MAX_DELAY``."""
    return float(min(_BASE_DELAY * (2**attempt), _MAX_DELAY))


class LinearConnector(TicketTrackerBase):
    """Thin Linear GraphQL adapter for resolve / read / scoped status+comment writes."""

    def __init__(self, token: str = "") -> None:
        if not token:
            raise ValueError("LinearConnector requires a 'token' credential (Linear API key)")
        self._token = token

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.LINEAR

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self._headers(),
            timeout=30,
        )

    async def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL operation with minimal retry/backoff.

        Raises ``ValueError`` for HTTP errors and for GraphQL ``errors`` payloads.
        Never logs the token or response bodies that may contain issue content.
        """
        payload = {"query": query, "variables": variables or {}}
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                body = await self._request_once(payload, attempt)
            except _RetrySignalError as sig:
                last_exc = sig.exc
                continue
            return self._extract_data(body)
        raise ValueError("Linear API request failed after retries") from last_exc

    async def _request_once(self, payload: dict[str, Any], attempt: int) -> dict[str, Any]:
        """Perform a single GraphQL HTTP request and return the parsed JSON body.

        Raises ``_RetrySignalError`` to request a retry on a transient condition, or
        ``ValueError`` on a terminal HTTP/transport failure.
        """
        try:
            async with self._client() as client:
                r = await client.post(_LINEAR_GRAPHQL_URL, json=payload)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_retry_delay(attempt))
                raise _RetrySignalError(exc) from exc
            raise ValueError(f"Linear API transport error: {exc}") from exc
        if r.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
            await asyncio.sleep(_retry_delay(attempt))
            raise _RetrySignalError(None)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                await asyncio.sleep(_retry_delay(attempt))
                raise _RetrySignalError(exc) from exc
            detail = exc.response.text[:200]
            raise ValueError(f"Linear API HTTP {exc.response.status_code}: {detail}") from exc
        body: dict[str, Any] = r.json()
        return body

    def _extract_data(self, body: dict[str, Any]) -> dict[str, Any]:
        """Validate a GraphQL response body and return its ``data`` payload.

        Raises ``ValueError`` when the body carries a GraphQL ``errors`` entry.
        """
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            first = errors[0] if isinstance(errors, list) and errors else errors
            message = first.get("message", "unknown GraphQL error") if isinstance(first, dict) else str(first)
            raise ValueError(f"Linear API error: {message}")
        return body.get("data", {}) or {}

    @staticmethod
    def _ref_from_filters(filters: dict[str, Any]) -> str:
        """Extract a non-empty string issue ref from a filter/data dict.

        Linear methods expect ``str``; the underlying dicts are untyped
        (``dict[str, Any]``) so values come back as ``Any | None``. Coerce to
        ``str`` so callers keep their strict signatures.
        """
        ref = filters.get("issue_ref") or filters.get("ticket_id")
        return str(ref) if ref is not None else ""

    @staticmethod
    def _issue_args(issue_ref: str) -> dict[str, Any]:
        """Map an issue_ref to Linear GraphQL ``issue`` resolver args.

        Linear's ``Query.issue`` accepts only a single required ``id`` argument
        (``String!``) — there is no ``identifier`` argument. Linear resolves
        both the internal UUID and the human ``TEAM-123`` identifier via ``id``,
        so the ref is always passed through ``id``.
        """
        issue_ref = (issue_ref or "").strip()
        if not issue_ref:
            raise ValueError("issue_ref is required")
        return {"id": issue_ref}

    async def _resolve_id(self, issue_ref: str) -> str:
        """Return the internal Linear issue id for *issue_ref* (id or identifier)."""
        args = self._issue_args(issue_ref)
        data = await self._graphql(
            """
            query ($id: ID!) {
              issue(id: $id) { id }
            }
            """,
            args,
        )
        issue = data.get("issue")
        if not issue:
            raise ValueError(f"Linear issue not found: {issue_ref}")
        return str(issue["id"])

    async def resolve(self, issue_ref: str) -> dict[str, Any]:
        """T1 - resolve an issue reference into a thin fact dict.

        Returns ``{id, identifier, title, status, assignee, link, updated_at}``.
        """
        args = self._issue_args(issue_ref)
        data = await self._graphql(
            """
            query ($id: ID!) {
              issue(id: $id) {
                id
                identifier
                title
                description
                state { name }
                assignee { name }
                url
                updatedAt
              }
            }
            """,
            args,
        )
        issue = data.get("issue")
        if not issue:
            raise ValueError(f"Linear issue not found: {issue_ref}")
        state = issue.get("state") or {}
        assignee = issue.get("assignee")
        return {
            "id": issue.get("id"),
            "identifier": issue.get("identifier"),
            "title": issue.get("title"),
            "status": state.get("name"),
            "assignee": assignee.get("name") if isinstance(assignee, dict) else None,
            "link": issue.get("url"),
            "updated_at": issue.get("updatedAt"),
        }

    async def get_issue_body(self, issue_ref: str) -> str:
        """T2 - fetch the issue description body (read-only)."""
        args = self._issue_args(issue_ref)
        data = await self._graphql(
            """
            query ($id: ID!) {
              issue(id: $id) { description }
            }
            """,
            args,
        )
        issue = data.get("issue")
        if not issue:
            raise ValueError(f"Linear issue not found: {issue_ref}")
        body = issue.get("description")
        return body if isinstance(body, str) else ""

    async def get_comments(self, issue_ref: str) -> list[dict[str, Any]]:
        """T2 - fetch the issue comments (read-only)."""
        args = self._issue_args(issue_ref)
        data = await self._graphql(
            """
            query ($id: ID!) {
              issue(id: $id) {
                comments { nodes { id body createdAt } }
              }
            }
            """,
            args,
        )
        issue = data.get("issue")
        if not issue:
            raise ValueError(f"Linear issue not found: {issue_ref}")
        comments = issue.get("comments") or {}
        nodes = comments.get("nodes") or []
        return [
            {
                "id": node.get("id"),
                "body": node.get("body"),
                "created_at": node.get("createdAt"),
            }
            for node in nodes
            if isinstance(node, dict)
        ]

    async def update_status(self, issue_ref: str, status: str) -> dict[str, Any]:
        """T3 - scoped status update.

        Resolves the target workflow state id by matching *status* (case
        insensitive) against the issue's team states, then issues a
        ``issueUpdate`` mutation. Returns the new status name.
        """
        if not status:
            raise ValueError("update_status requires a status")
        issue_id = await self._resolve_id(issue_ref)
        states = await self._fetch_team_states(issue_ref)
        target = self._match_state(states, status)
        if target is None:
            available = ", ".join(s.get("name", "") for s in states)
            raise ValueError(f"Linear status {status!r} not found (available: {available})")
        data = await self._graphql(
            """
            mutation ($issueId: String!, $stateId: String!) {
              issueUpdate(id: $issueId, input: { stateId: $stateId }) {
                success
                issue { id state { name } }
              }
            }
            """,
            {"issueId": issue_id, "stateId": target["id"]},
        )
        payload = (data.get("issueUpdate") or {}).get("issue") or {}
        new_state = payload.get("state") or {}
        return {
            "issue_id": payload.get("id"),
            "status": new_state.get("name"),
        }

    async def _fetch_team_states(self, issue_ref: str) -> list[dict[str, Any]]:
        args = self._issue_args(issue_ref)
        data = await self._graphql(
            """
            query ($id: ID!) {
              issue(id: $id) {
                team { states { nodes { id name type } } }
              }
            }
            """,
            args,
        )
        issue = data.get("issue") or {}
        team = issue.get("team") or {}
        states = team.get("states") or {}
        nodes = states.get("nodes") or []
        return [s for s in nodes if isinstance(s, dict)]

    @staticmethod
    def _match_state(states: list[dict[str, Any]], status: str) -> dict[str, Any] | None:
        wanted = status.strip().lower()
        for state in states:
            name = state.get("name") or ""
            if name.strip().lower() == wanted:
                return state
        return None

    async def _post_comment(self, issue_ref: str, body: str) -> dict[str, Any]:
        """T3 - create a comment on the issue (prefixed body provided by base)."""
        issue_id = await self._resolve_id(issue_ref)
        data = await self._graphql(
            """
            mutation ($input: CommentCreateInput!) {
              commentCreate(input: $input) {
                success
                comment { id body }
              }
            }
            """,
            {"input": {"issueId": issue_id, "body": body}},
        )
        result = data.get("commentCreate") or {}
        comment = result.get("comment") or {}
        return {
            "issue_id": issue_id,
            "comment_id": comment.get("id"),
            "body": comment.get("body"),
        }

    async def health_check(self) -> HealthResult:
        """Verify the API key by querying the authenticated viewer."""
        try:
            data = await self._graphql("query { viewer { id name } }")
            viewer = data.get("viewer") or {}
            return HealthResult(ok=True, detail=viewer.get("name") or "ok")
        except ValueError as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return HealthResult(ok=False, detail=str(exc)[:200])

    async def get_ticket(self, ticket_id: str) -> Ticket:
        """Resolve an issue into the shared :class:`Ticket` shape (T1 surface)."""
        fact = await self.resolve(ticket_id)
        return Ticket(
            id=fact.get("id") or fact.get("identifier") or ticket_id,
            title=fact.get("title") or "",
            description=None,
            status=fact.get("status") or "unknown",
            ticket_type="task",
            labels=[],
            url=fact.get("link"),
            assignee=fact.get("assignee"),
            updated_at=None,
            raw=fact,
        )

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        """Thin read surface routed by resource name."""
        filters = q.filters or {}
        match q.resource:
            case "issue":
                if "ticket_id" not in filters and "issue_ref" not in filters:
                    raise ValueError("Linear issue query requires 'issue_ref' (or 'ticket_id') filter")
                ref = self._ref_from_filters(filters)
                fact = await self.resolve(ref)
                return ConnectorResult(records=[fact], total=1)
            case "comments":
                if "ticket_id" not in filters and "issue_ref" not in filters:
                    raise ValueError("Linear comments query requires 'issue_ref' (or 'ticket_id') filter")
                ref = self._ref_from_filters(filters)
                return ConnectorResult(records=await self.get_comments(ref), total=None)
            case "issue_body":
                if "ticket_id" not in filters and "issue_ref" not in filters:
                    raise ValueError("Linear issue_body query requires 'issue_ref' (or 'ticket_id') filter")
                ref = self._ref_from_filters(filters)
                return ConnectorResult(records=[{"body": await self.get_issue_body(ref)}], total=1)
            case _:
                raise ValueError(f"Unsupported Linear query resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        """Thin scoped-write surface routed by resource name."""
        data = payload.data
        match payload.resource:
            case "comment":
                if "issue_ref" not in data and "ticket_id" not in data:
                    raise ValueError("Linear comment write requires 'issue_ref' (or 'ticket_id') in data")
                if "body" not in data:
                    raise ValueError("Linear comment write requires 'body' in data")
                ref = self._ref_from_filters(data)
                return await self.comment(ref, data["body"])
            case "status_update":
                if "issue_ref" not in data and "ticket_id" not in data:
                    raise ValueError("Linear status_update write requires 'issue_ref' (or 'ticket_id') in data")
                if "status" not in data:
                    raise ValueError("Linear status_update write requires 'status' in data")
                ref = self._ref_from_filters(data)
                return await self.update_status(ref, data["status"])
            case _:
                raise ValueError(f"Unsupported Linear write resource: {payload.resource!r}")
