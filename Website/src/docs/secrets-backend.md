---
title: Secrets Backend
description: Pluggable secret storage — encrypt and store connector credentials, API keys, and sensitive values.
---

# Secrets Backend

Modulo provides a pluggable `SecretsBackend` interface for encrypting and
storing sensitive values. Three implementations exist:

- **Fernet** (default) — encrypts at rest with `cryptography.fernet`, stored in the database
- **HashiCorp Vault** — KV v2 engine via the `hvac` library
- **AWS Secrets Manager** — via the `boto3` library

The active backend is selected via the `MODULO_SECRETS_BACKEND` environment
variable or the `create_secrets_backend()` factory.

## Usage

Secrets are managed through the `get_secret`, `set_secret`, and `delete_secret`
interface. All secrets are encrypted at rest (Fernet) or stored in the
configured external backend (Vault / AWS).

For the full specification, see the [PRD §7.13](/prd#7-13-secrets-backend).
