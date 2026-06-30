---
id: feat-core-secrets-backend
prd: 7.13
delivery-tasks: [task-nv10-secrets-backend]
bdd:
  - backend/tests/bdd/features/security/credential_store.feature
code:
  - backend/src/modulo/core/secrets_backend/__init__.py
  - backend/src/modulo/core/secrets_backend/fernet.py
  - backend/src/modulo/core/secrets_backend/vault.py
  - backend/src/modulo/core/secrets_backend/aws.py
  - backend/tests/unit/secrets_backend/test_factory.py
  - backend/tests/unit/secrets_backend/test_fernet_backend.py
  - backend/tests/unit/secrets_backend/test_vault_backend.py
  - backend/tests/unit/secrets_backend/test_aws_backend.py
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

### Edge cases

- [ ] `delete_secret` is a no-op when the key does not exist (all three backends)
- [ ] `set_secret` on a previously deleted key creates a fresh record (no tombstone conflict)
- [ ] Fernet backend: org ID is cached after the first `_read_org_id_from_session` call and reused on subsequent operations
- [ ] Factory: `fernet_key` is `None` for Vault/AWS backends (not required)
- [ ] Factory: `backend_name` is case-insensitive and whitespace-trimmed
- [ ] Vault backend: secret path is constructed as `{VAULT_PATH_PREFIX}/{key}`
- [ ] AWS backend: `SecretBinary` decoded as UTF-8 fallback (not just `SecretString`)
- [ ] Fernet backend: `set_session()` can replace the DB session after construction

### Error states

- [ ] `get_secret` with a non-existent key raises `KeyError` (all three backends)
- [ ] `get_secret` on corrupted/undecryptable data raises `ValueError` (Fernet backend, `InvalidToken`)
- [ ] Any operation on Fernet backend with missing org session returns error
- [ ] Vault backend: connection failure raises `VaultError` with detail
- [ ] AWS backend: `get_secret` on non-existent key raises `KeyError`
- [ ] AWS backend: `delete_secret` on non-existent key raises `KeyError`
- [ ] Factory: invalid `backend_name` raises `ValueError` with available backends listed

## Known Gaps

- No BDD feature file exists for secrets backend behaviour
- `delete_secret` behaviour on non-existent key differs across backends (no-op vs raises)
- No key rotation schedule or automatic re-encryption
- No audit event emitted on secret read/write/delete
- No secret expiry / TTL support
- Fernet backend stores encrypted values in the same DB as application data — no HSM or external KMS
