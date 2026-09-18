---
id: feat-core-secrets-backend
prd: N/A
adr: []
code:
  - backend/src/modulo/core/secrets_backend/__init__.py
  - backend/src/modulo/core/secrets_backend/fernet.py
  - backend/src/modulo/core/secrets_backend/vault.py
  - backend/src/modulo/core/secrets_backend/aws.py
  - backend/src/modulo/core/fernet_rotation.py
unit-tests:
  - backend/tests/unit/secrets_backend/test_factory.py
  - backend/tests/unit/secrets_backend/test_fernet_backend.py
  - backend/tests/unit/secrets_backend/test_vault_backend.py
  - backend/tests/unit/secrets_backend/test_aws_backend.py
  - backend/tests/unit/secrets_backend/test_run_sync.py
  - backend/tests/unit/core/test_fernet_rotation.py
bdd: []
depends-on: []
status: covered
---

# Secrets Backend

The pluggable at-rest secret store for credential material (connector
credentials, model-backend API keys, notification endpoints and OTel export
keys). The ``SecretsBackend`` ABC exposes async ``get_secret`` / ``set_secret``
/ ``delete_secret``; the factory selects ``fernet`` (default, Fernet-encrypted
rows in the ``secrets`` table), ``vault`` (HashiCorp Vault) or ``aws`` (Secrets
Manager), with a license gate for external backends. ``fernet_rotation.py``
re-encrypts every encrypted store to a new key with an ``old_key`` fallback for
no-downtime rotation. Infra-only surface — no UI route, so it is tracked here
rather than in the manifest registry.

## Behaviours

- [x] ``SecretsBackend`` ABC with async ``get_secret`` / ``set_secret`` /
      ``delete_secret`` and a ``KeyError`` contract for a missing key
- [x] Secret keys are validated / normalised (non-empty string, stripped) —
      invalid keys raise ``ValueError``, never touch the store
- [x] Factory ``create_secrets_backend`` selects the backend from
      ``backend_name`` / ``MODULO_SECRETS_BACKEND`` (``fernet`` default,
      ``vault``, ``aws``); unknown name → ``ValueError`` listing the choices
- [x] External backends (``vault`` / ``aws``) are license-gated via the
      centralized ``external_secrets`` feature flag and fall back to
      ``fernet`` when unlicensed (logged)
- [x] ``fernet`` backend encrypts values with ``cryptography.fernet.Fernet``
      and persists them in the ``secrets`` table with an
      ``(organisation_id, key)`` scope
- [x] ``fernet`` backend is RLS-aware — reads the organisation id from the
      session (``app.organisation_id`` via ``current_setting``, falling back
      to ``session.info`` on non-Postgres backends) and scopes every row to
      that org; missing org context fails closed with an actionable error
- [x] ``set_secret`` upserts under ``(organisation_id, key)`` with a
      row-lock + TOCTOU retry (``IntegrityError`` → one retry, then re-raise)
- [x] ``get_secret`` retrieves and decrypts; a stored value the current key
      cannot decrypt is re-tried with an optional old key (no-downtime
      rotation) and fails with ``ValueError`` when no key matches
- [x] Invalid Fernet keys surface a friendly config ``ValueError`` at
      construction, never an opaque ``binascii`` error
- [x] ``vault`` / ``aws`` backends call out synchronously via ``run_sync``
      (bounded thread-pool execution with a configurable timeout)
- [x] Implementations never log or leak secret values in exceptions,
      tracebacks or span attributes
- [x] ``fernet_rotation`` — no-downtime key rotation: ``decrypt_with_fallback``
      decays to the old key, ``re_encrypt_*`` re-wraps plaintext under the new
      key, and ``rotate_all_encrypted_data`` re-encrypts every encrypted store
      (secrets table, connector instances, model backends, notification
      endpoints, OTel config, checkpoints / checkpoint blobs / writes),
      returning per-store counts

## Known Gaps

- **No BDD feature files.** Backend behaviour is unit-tested
  (``backend/tests/unit/secrets_backend/*`` + ``test_fernet_rotation.py``);
  there is no pytest-bdd coverage and no UI route.
- **External backends require a paid plan.** ``vault`` / ``aws`` are
  license-gated; ``fernet`` is the only community-tier store.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — entry added to
  close the feature-graph gap for a shipped infra-only surface consumed by the
  connector / model-backend / notification / OTel-export credential paths but
  invisible to the product map. Behaviours verified against
  ``core/secrets_backend/*``, ``core/fernet_rotation.py`` and the
  ``secrets_backend`` + ``fernet_rotation`` unit suites. Status: covered.
