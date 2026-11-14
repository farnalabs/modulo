# Modulo operations

Use the surface that matches the deployment and task. Public references: <https://modulo.run/docs/installation>, <https://modulo.run/docs/mcp>, and <https://github.com/farnalabs/farna-control>.

## Native launcher

The documented native launcher commands include:

```bash
modulo start
modulo stop
modulo status
modulo version
modulo doctor --json
modulo support
modulo env
modulo logs app
modulo clear-degraded
modulo upgrade <target-version>
```

Use `modulo doctor --json` and `modulo status` for read-only health checks. `modulo logs` is for diagnosis; avoid copying secrets or authorization headers into reports. `modulo clear-degraded` and `modulo upgrade` change state: show the target and effect first.

The native launcher also exposes backup/restore workflows documented in the public operations documentation. Do not invent flags; read the matching public guide before running one.

## Declarative configuration

`modulo apply -f <config.yaml>` is the documented declarative configuration CLI. `--plan`/`--dry-run` and `--diff` are read-only planning/drift modes; a real apply writes schemas, model backends, pipelines, and triggers. It uses `MODULO_URL` and `MODULO_API_KEY` supplied through the machine environment, not inline secret values. Verify the target before applying.

## Remote MCP

The Remote MCP endpoint is `/mcp`. Configure its canonical external URL and credential explicitly through Claude Code. Prefer read-only tools for discovery. Machine credentials are for unattended operations. Human-only HITL gates remain human decisions: do not use MCP or a human-linked credential to claim, approve, reject, or deliver such a gate.

## Web UI and API

Use the web UI for first-run administration and account/user setup. Use the documented API or Remote MCP for automation. Do not infer authentication from a page load, open port, or status code alone; verify the returned identity and organisation using a read-only authenticated operation.
