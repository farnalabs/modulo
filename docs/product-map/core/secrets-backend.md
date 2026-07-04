---
id: feat-core-secrets-backend
prd: 7.13
delivery-tasks: [task-nv10-secrets-backend]
bdd: [backend/tests/bdd/features/security/credential_store.feature]
code:
  - backend/src/modulo/core/secrets_backend/__init__.py
  - backend/src/modulo/core/secrets_backend/fernet.py
  - backend/src/modulo/core/secrets_backend/vault.py
  - backend/src/modulo/core/secrets_backend/aws.py
  - backend/tests/unit/secrets_backend/test_factory.py
  - backend/tests/unit/secrets_backend/test_fernet_backend.py
  - backend/tests/unit/secrets_backend/test_vault_backend.py
  - backend/tests/unit/secrets_backend/test_aws_backend.py
depends-on: [feat-core-db-abstraction-core]
unit-tests: [backend/tests/unit/secrets_backend/test_factory.py, backend/tests/unit/secrets_backend/test_fernet_backend.py, backend/tests/unit/secrets_backend/test_vault_backend.py, backend/tests/unit/secrets_backend/test_aws_backend.py]
status: partial
---

# Secrets Backend — Pluggable Secret Storage

The `SecretsBackend` ABC defines a uniform interface (`get_secret`, `set_secret`, `delete_secret`) for encrypting and storing connector credentials, model backend API keys, and other sensitive values. Three implementations exist: **Fernet** (default, DB-backed), **HashiCorp Vault** (KV v2), and **AWS Secrets Manager** — selected via `MODULO_SECRETS_BACKEND` env var or the `create_secrets_backend()` factory.

## Behaviours

### Happy path

- [x] `get_secret` retrieves a previously stored plaintext value for an existing key
- [x] `set_secret` stores a new key-value pair, encrypting the value at rest
- [x] `set_secret` overwrites an existing key with a new value (upsert semantics)
- [x] `delete_secret` removes the record for an existing key
- [x] Fernet backend: encrypted value round-trips through `set_secret` → `get_secret` returning the original plaintext
- [x] Factory `create_secrets_backend()` returns `FernetSecretsBackend` by default (no env var, no `backend_name`)
- [x] Factory respects the `MODULO_SECRETS_BACKEND` env var when no `backend_name` is passed
- [x] Fernet backend: `FERNET_KEY` is separate from `SECRET_KEY` (distinct cryptographic domains — JWT signing vs secret encryption)
- [x] Vault backend: reads/writes secrets under the configured `VAULT_PATH_PREFIX`
- [x] AWS backend: creates a new secret via `create_secret`, updates existing via `update_secret`
- [x] AWS backend: `verify` credential path on delete (7-day recovery window, no force-delete)
- [x] `get_secret` filters by organisation_id (RLS-safe on generic backends)
- [x] `delete_secret` filters by organisation_id (RLS-safe on generic backends)

### Edge cases

- [x] `delete_secret` is a no-op when the key does not exist (all three backends)
- [x] `set_secret` on a previously deleted key creates a fresh record (no tombstone conflict)
- [x] Fernet backend: org ID is cached after the first `_read_org_id_from_session` call and reused on subsequent operations
- [x] Factory: `fernet_key` is `None` for Vault/AWS backends (not required)
- [x] Factory: `backend_name` is case-insensitive and whitespace-trimmed
- [x] Vault backend: secret path is constructed as `{VAULT_PATH_PREFIX}/{key}`
- [x] AWS backend: `SecretBinary` decoded as UTF-8 fallback (not just `SecretString`)
- [x] Fernet backend: `set_session()` can replace the DB session after construction

### Error states

- [x] `get_secret` with a non-existent key raises `KeyError` (all three backends)
- [x] `get_secret` on corrupted/undecryptable data raises `ValueError` (Fernet backend, `InvalidToken`)
- [x] Any operation on Fernet backend with missing org session returns error
- [ ] Vault backend: connection failure raises `VaultError` with detail
- [x] AWS backend: `get_secret` on non-existent key raises `KeyError`
- [x] AWS backend: `delete_secret` on non-existent key is a no-op (same as all other backends)
- [x] Factory: invalid `backend_name` raises `ValueError` with available backends listed

### Error Handling

- [x] Vault calls have 30s timeout (`asyncio.wait_for` with `_TIMEOUT = 30.0`)
- [x] AWS calls have 30s timeout (`asyncio.wait_for` with `_TIMEOUT = 30.0`)
- [x] Vault `get_secret` timeout wraps as `RuntimeError("VaultSecretsBackend: timeout reading secret")`
- [x] Vault `set_secret` timeout wraps as `RuntimeError("VaultSecretsBackend: timeout writing secret")`
- [x] Vault `delete_secret` timeout wraps as `RuntimeError("VaultSecretsBackend: timeout deleting secret")`
- [x] AWS `get_secret` timeout wraps as `RuntimeError("AWSSecretsManagerBackend: timeout reading secret")`
- [x] AWS `set_secret` timeout wraps as `RuntimeError("AWSSecretsManagerBackend: timeout writing secret")`
- [x] AWS `delete_secret` timeout wraps as `RuntimeError("AWSSecretsManagerBackend: timeout deleting secret")`
- [x] Vault network errors wrap as `RuntimeError("VaultSecretsBackend: unexpected error reading/writing/deleting secret: {exc}")`
- [x] AWS network errors wrap as `RuntimeError("AWSSecretsManagerBackend: unexpected error reading/writing/deleting secret: {exc}")`

### Edge Cases (additional)

- [ ] Empty key string behaviour (all backends)
- [ ] Null bytes in secret values (Fernet encryption round-trip)
- [ ] Concurrent delete+set race (two concurrent sessions operating on same key)
- [ ] Invalid Fernet key at construction (`ValueError` from `cryptography.fernet.Fernet`)
- [ ] Org ID caching expiry / reset on session change

### Credential-in-state rule (PRD §7.13)

- [x] Decrypted credentials never enter LangGraph StateGraph state — enforced via lint rule banning credential field names from state dict assignments
- [x] Decrypted credentials never appear in checkpoint blobs — `generate_secrets_filter()` strips sensitive keys before state is persisted
- [x] Decrypted credentials never appear in OTel span attributes — `test_observability.py:test_trace_no_credentials` validates no credential fields in spans
- [x] Connectors receive decrypted credential in-process only via transient context object, used for API call, not serialised

## QA History

### 2026-07-04 — Cross-cutting QA (improve-architecture index 140)

- **CRITICAL:** Added `organisation_id` filter to `FernetSecretsBackend.get_secret()` and `delete_secret()` — prevents cross-org data leak on generic/SQLite backends where RLS is not active
- **CRITICAL:** Added 30s timeout to all Vault and AWS async calls via `asyncio.wait_for` — prevents indefinite hangs on network failures
- **MAJOR:** Added broad `except Exception` handling to Vault and AWS backends for network errors and timeouts
- **MAJOR:** Added unit tests for org_id scoping in get/delete_secret and timeout/error handling in Vault/AWS backends
- **MAJOR:** Verified existing behaviour checkboxes (`set_secret` upsert, factory case-insensitivity, Vault path construction, AWS binary decode)

## Known Gaps

- BDD feature file exists (credential_store.feature, 3 scenarios) but does not exercise the pluggable backend interface — only Fernet. Add Vault/AWS BDD scenarios.
- No key rotation schedule or automatic re-encryption
- PRD §7.13 credential-in-state rule (decrypted credentials must never enter LangGraph state, checkpoints, OTel spans) is not tracked in this product map entry.
- No audit event emitted on secret read/write/delete
- No secret expiry / TTL support
- Fernet backend stores encrypted values in the same DB as application data — no HSM or external KMS
