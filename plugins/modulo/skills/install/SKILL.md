---
name: install
description: Install Modulo locally or connect Claude Code to an existing self-hosted Modulo instance. Use for first-time setup, onboarding, Remote MCP configuration, or changing the selected instance.
---

# Install or connect Modulo

This skill is a decision guide, not a second installer. Use the documented Modulo installer, Docker Compose, Helm/Kubernetes, web UI, API, and CLI surfaces rather than inventing commands.

## Choose the deployment shape

Ask which shape fits the user's environment:

- **Native workstation evaluation on Linux:** use the published `scripts/install.sh` flow described in the public installation docs. The current repository script is Linux-only; macOS uses the separately documented signed `.dmg` path. Do not claim native installation is available on Windows.
- **Current macOS artifacts:** the published release currently exposes an Apple Silicon (`arm64`) `.dmg`; do not promise an Intel (`x86_64`) download unless the selected release page lists one. Check the [public installation guide](https://modulo.run/docs/installation) and [releases page](https://github.com/farnalabs/farna-control/releases) for the release-specific artifact list.
- **Docker Compose:** use for a self-hosted server or production single machine. Follow the public installation/deployment documentation and keep `.env` credentials out of chat and source control.
- **Kubernetes:** use the maintained Helm chart for multi-replica deployments, with PostgreSQL and the documented Redis choice. Follow the public Kubernetes guide.
- **Existing remote self-host:** connect to its canonical HTTPS base URL. Reverse proxies, SSH tunnels, cloud hosts, and multiple instances are supported connection contexts; do not assume their topology.

Public docs: <https://modulo.run/docs/installation> and <https://modulo.run/docs/kubernetes>.

## Setup sequence

1. Choose or install the instance.
2. Verify health using the deployment's documented health/readiness path. For native installs, `modulo doctor --json` reports exit state 0 healthy, 1 unhealthy, 2 degraded, or 3 uninitialized; `modulo status` shows component state. An open port is not proof of health or authentication.
3. Initialise the instance and create the first admin/users through the documented UI or CLI path. Do not invent seed credentials and do not put passwords in prompts.
4. Record the canonical hostname/base URL, including the externally reachable scheme and path used by the reverse proxy. Confirm it identifies the intended instance and organisation.
5. Configure Remote MCP explicitly in Claude Code using the supported Claude configuration flow. The plugin's `.mcp.json` has an empty URL placeholder and therefore does not connect by itself.
6. Perform a read-only MCP discovery call first. Before a write, show the target and effect and confirm the selected account and organisation.

## Credentials

Never mint or request raw credentials in chat. Create or retrieve an appropriate machine/API credential through the documented Modulo UI or API-key flow, and configure it through Claude's supported MCP configuration mechanism. Do not paste bearer tokens into context files, skill files, shell history, logs, or issue descriptions. Do not use a human-linked credential for unattended work or for human-only HITL decisions.

## Re-connecting or changing instances

If more than one instance is configured, list the candidates and ask the user to choose. Never silently replace a configured URL or organisation. If context is stale, re-run read-only discovery and health checks; treat `.modulo/context.md` and `~/.modulo/context.md` as advisory only.

## References

- `${CLAUDE_PLUGIN_ROOT}/references/onboarding.md` — setup and verification checklist
- `${CLAUDE_PLUGIN_ROOT}/references/context-resolution.md` — target discovery and precedence
- `${CLAUDE_PLUGIN_ROOT}/references/context-schema.md` — safe advisory context format
- `${CLAUDE_PLUGIN_ROOT}/references/recovery.md` — stale, degraded, or failed setup
