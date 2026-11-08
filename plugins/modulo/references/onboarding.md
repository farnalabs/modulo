# Modulo onboarding

Use this checklist after choosing a deployment shape. Public user documentation is at <https://modulo.run/docs/installation>; the Remote MCP guide is at <https://modulo.run/docs/mcp>.

## 1. Choose or install

- Native Linux workstation: save the published installer to a file, inspect it, and run the documented `bash modulo-install.sh` flow. Do not pipe the installer directly into a shell; the installer verifies downloads before extraction.
- Docker Compose: follow the public deployment guide, create the required environment values through the documented path, and keep `.env` out of source control.
- Kubernetes: use the public Helm/Kubernetes guide and the operator's cluster credentials outside chat.
- Existing remote instance: use its operator-provided canonical HTTPS URL. For SSH or reverse-proxy access, verify the externally reachable URL rather than treating an internal address as canonical.

## 2. Verify

For native installs, use:

```bash
modulo doctor --json
modulo status
```

`doctor --json` has documented exit states: 0 healthy, 1 unhealthy, 2 degraded, and 3 uninitialized. For Compose or Kubernetes, use the deployment's documented readiness/health checks and the web UI. A successful TCP connection or an unauthenticated HTTP response is not enough.

## 3. Initialise and identify

Complete first-run initialization and create the admin/users through the documented UI or CLI flow. Then write down the canonical hostname/base URL and the intended organisation. Verify that the URL and account describe the same instance before changing anything.

## 4. Configure MCP

The Modulo Remote MCP server is mounted at `/mcp` and uses a bearer API key or OAuth as documented at <https://modulo.run/docs/mcp>. Configure the real endpoint and credential explicitly through Claude Code's supported MCP configuration flow. The plugin placeholder is intentionally `{ "url": "" }`; it remains disconnected until configured.

Do not place the key in `.modulo/context.md`, `~/.modulo/context.md`, prompts, logs, source files, or chat. Start with a read-only MCP operation and show the target before writes.

## 5. Persist only advisory metadata

If useful, record the non-secret URL, deployment shape, and display name in `.modulo/context.md` or `~/.modulo/context.md` using `context-schema.md`. These files are hints for future discovery, not authority or automatic configuration.
