# Secret Management

## How Secrets Are Stored

Modulo supports three backends for secret storage, configured via the `MODULO_SECRETS_BACKEND` environment variable:

| Backend | Env Value | Description |
|---------|-----------|-------------|
| Fernet (default) | `fernet` | Encrypted at rest in the database using `FERNET_KEY`. Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256). The encryption key must be exactly 32 base64-encoded bytes. |
| HashiCorp Vault | `vault` | KV v2 secrets engine via `hvac`. Configured via `VAULT_ADDR`, `VAULT_TOKEN` or `VAULT_ROLE_ID`+`VAULT_SECRET_ID`. |
| AWS Secrets Manager | `aws` | Managed via `boto3`. IAM role or access key determines access. |

At startup, Modulo reads `SECRET_KEY` (for JWT signing, minimum 32 bytes) and `FERNET_KEY` (for Fernet encryption, exactly 32 base64-encoded bytes). The application refuses to start if either is absent or insufficient.

Connector credentials and webhook secrets are decrypted once per run into a run-scoped context object. Decrypted values **never** enter:

- LangGraph state or checkpoint blobs
- OTel span attributes
- Application logs
- API responses (masked with `●●●●●` by default, with a 30-second server-authenticated reveal option)

## What NOT to Commit

The following must never appear in the repository:

- `SECRET_KEY`: JWT signing key
- `FERNET_KEY`: Fernet encryption key
- `DATABASE_URL`: database connection string with credentials
- `MODULO_DB`: database type indicator
- Any `MODULO_*_KEY`: API keys for integrations
- `.env` files: environment variable dumps
- Service account keys or tokens
- Any file containing plaintext credentials

Gitleaks runs as a pre-commit hook and in CI to catch accidental commits of these patterns. If gitleaks blocks your commit, see the false positive handling section below.

## How to Rotate Secrets

### SECRET_KEY / FERNET_KEY

1. Generate a new key:
   - `SECRET_KEY`: any 32+ byte random value (`openssl rand -base64 32`)
   - `FERNET_KEY`: 32 base64-encoded bytes (`python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
2. Update the environment variable in your deployment configuration.
3. Restart Modulo services (rolling restart recommended).
4. Verify: check application logs for successful startup and JWT validation.

**Note**: Rotating `SECRET_KEY` invalidates all existing JWT sessions. Rotating `FERNET_KEY` does not re-encrypt existing secrets: use `modulo restore <backup-dir> --previous-fernet-key <old-key>` to re-encrypt stored credentials under the new key.

### Vault Secrets

1. Log into Vault UI or CLI.
2. Navigate to the secret path configured for Modulo.
3. Update the secret values.
4. Modulo picks up the changes on the next decrypt cycle (per-run).

### AWS Secrets Manager

1. Use the AWS Console, CLI, or SDK to update the secret value.
2. Optionally configure automatic rotation via AWS Secrets Manager rotation schedules.

## Incident Response: Leaked Secret Procedure

If a secret is accidentally committed and pushed:

1. **Immediately revoke the leaked secret**: rotate the affected credential (see rotation steps above).
2. **Remove the secret from git history** using `git filter-branch` or `bfg-repo-cleaner`.
3. **Force-push the cleaned history** (coordinate with your team to rebase open branches).
4. **Audit access logs**: check for unauthorized access between the leak time and revocation.
5. **Update the allowlist** if the leak was from a path that should be excluded from scanning.

## Gitleaks False Positive Handling

Gitleaks may flag legitimate test fixtures, config templates, or documentation examples. To suppress a false positive:

### Per-File Allowlisting

Add the file path to the `[allowlist]` section in `.gitleaks.toml` at the repository root:

```toml
[allowlist]
paths = [
    "tests/",
    "docs/examples/",
]
```

### Per-Line Inline Ignore (Gitleaks v8.18+)

Add a gitleaks ignore comment to the specific line:

```python
# gitleaks:allow
SECRET_KEY = "placeholder-for-testing-only"
```

### Commit-Level Silence (Emergency Only)

If gitleaks is blocking a legitimate commit and you need to bypass temporarily:

```bash
git commit --no-verify -m "message"
```

Then file an issue to fix the allowlist permanently.
