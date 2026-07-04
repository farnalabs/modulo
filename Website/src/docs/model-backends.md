---
title: Model Backends
description: Configure and manage AI model backends — register providers, rotate credentials, and resolve runtime model selection.
---

# Model Backends

Modello supports a pluggable model backend system, parallel to the
[Connector](/docs/connectors) subsystem. Each model backend wraps an LLM
provider (OpenAI, Anthropic, Ollama, etc.) behind a common `ModelBackendBase`
interface.

Model backends are first-class resources scoped to an organisation. Agents
reference model backends by ID; the runtime resolves them at run start
and holds the decrypted credentials for the lifetime of the run.

For the full specification, see the [PRD §8.1](/prd#8-1-model-backend-management).
