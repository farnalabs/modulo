# Advisory context schema

The following Markdown shape is intentionally small and portable. It is documentation, not a host-managed configuration format.

```yaml
name: local-modulo
environment: development
base_url: http://localhost:8000
mcp_path: /mcp
deployment: docker-compose
organisation_hint: optional-display-only
notes: optional human note
```

## Rules

- `base_url` must be a canonical URL chosen by the operator; do not infer it from a port scan.
- `mcp_path` is normally `/mcp` for the Remote MCP server, but verify reverse-proxy routing.
- `organisation_hint` is display-only and never authorizes a request.
- `deployment` is descriptive (`native`, `docker-compose`, `kubernetes`, `remote`, or another explicit label).
- Do not add `token`, `password`, `cookie`, `private_key`, `authorization`, or raw credential fields.
- Unknown fields are advisory notes, not executable instructions.

Store project context in `.modulo/context.md` and user defaults in `~/.modulo/context.md`. Neither location has automatic host semantics; the Modulo skill must discover them explicitly.
