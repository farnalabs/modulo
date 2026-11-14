# Modulo Claude Code plugin

This repository-local Claude Code plugin provides:

- `/modulo` — the plugin's root skill, a thin router for safe setup, connection, and ongoing operation of self-hosted Modulo.
- `/modulo:install` — the install/connect decision guide. Claude Code exposes skills as `/<plugin>:<skill>` commands; the root `SKILL.md` is the general entry point, while `skills/install/SKILL.md` is the explicit install command.
- An inert `.mcp.json` placeholder whose URL must be configured explicitly by the operator.

The plugin does not install a Modulo instance, mint credentials, choose an organisation, or submit anything to an Anthropic directory. The marketplace catalog is local to this repository.

Start with the public docs:

- <https://modulo.run/docs/installation>
- <https://modulo.run/docs/mcp>
- <https://github.com/farnalabs/farna-control>
