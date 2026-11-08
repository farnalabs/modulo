# Modulo recovery

Stop before mutation when the target is stale, degraded, ambiguous, or unexpectedly authenticated. Public troubleshooting reference: <https://modulo.run/docs/troubleshooting>.

## Stale context or wrong instance

1. Re-read project and user context explicitly.
2. Compare candidate base URLs and deployment labels.
3. Run the documented read-only health check against the selected target.
4. Verify the returned identity/organisation through an authenticated read-only surface.
5. Ask the user to choose if candidates still disagree. Never silently switch.

## Native degraded or uninitialized state

Run:

```bash
modulo doctor --json
modulo status
```

Exit state 3 means uninitialized; follow the documented first-run initialization path. Exit state 2 means degraded; inspect `modulo status` and `modulo logs app` before using `modulo clear-degraded`. Do not clear a degraded state merely to make a check green.

## Remote or Compose failure

Use the deployment-specific public troubleshooting and support guides. Check readiness, application logs, PostgreSQL, Redis, reverse-proxy routing, and the configured external URL. Do not probe arbitrary private networks, Docker sockets, Kubernetes APIs, or cloud metadata endpoints.

## MCP does not connect

An empty plugin URL is expected and inert. Confirm the configured endpoint is the canonical external `/mcp` URL, then confirm the credential was configured through Claude's supported MCP flow. A port being open is not proof of a valid MCP route or credential. Start with a read-only tool call and do not paste the credential into diagnostics.

## Recovery writes

Before any repair, show the target, selected organisation/account, exact effect, and rollback/backup path. Use machine credentials for unattended work. Never approve, reject, or deliver a human-only HITL review through an agent credential.
