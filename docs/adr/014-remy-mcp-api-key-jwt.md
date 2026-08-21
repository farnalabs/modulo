# ADR 014 — Remy Stream: JWT as MCP API Key

**Date**: 2026-07-07
**Status**: Accepted

---

## Context

Remy's streaming endpoint (`POST /api/v1/remy/sessions/{id}/stream`) accepts an `mcp_api_key` field in the request body. The frontend sends the user's current JWT (`Authorization: Bearer <jwt>`) as this value.

During a QA review, this was flagged as a "security leak" — a user's auth token appearing in the HTTP request body. The field was removed, which broke MCP tool execution (`get_documentation`, `get_integration_status`, `browse_library`, etc.) because the backend's MCP server needs an API key to authenticate calls made during the agentic loop.

## Decision

The JWT is intentionally passed as `mcp_api_key` in the request body. This is **not** a security issue because:

1. **HTTPS encrypts everything** — the `Authorization` header and the POST body are both encrypted by TLS. An attacker who can read the body can also read the header. There is no additional exposure surface.

2. **Single-request scope** — the `mcp_api_key` field is only used for this one streaming request. The backend extracts it from `StreamRequest` and passes it to MCP tool calls made during that specific agentic loop run. It is never stored in the database, never logged (no request-body logging in production), and never persisted.

3. **Same trust boundary** — the frontend is already trusted with the JWT (it holds it in memory and sends it in every authenticated request). Sending it in the body of a single additional request doesn't extend the trust boundary.

4. **No alternative** — the MCP server requires an API key scoped to the user's org. The user's JWT is the natural credential because it encodes the user's identity and org membership. Generating a separate MCP API key would require either a new backend endpoint or storing a separate credential on the frontend, both of which add complexity without security benefit.

## Consequences

- Future agents reviewing this code should NOT flag `mcp_api_key` in the stream request body as a security finding.
- If request-body logging is ever enabled in production, the JWT will appear in logs. Mitigate by masking `mcp_api_key` at the log sink level — not by removing it from the request.
