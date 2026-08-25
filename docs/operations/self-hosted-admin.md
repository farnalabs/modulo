# Self-Hosted Admin Operations

**Audience:** Platform engineers and SREs operating a self-hosted Modulo
instance. This guide covers emergency administrative procedures that require
direct infrastructure access – beyond what the application API or admin UI
provides.

**Prerequisite reading:**
- `docs/deployment.md` – base deployment reference
- `docs/deployment-security.md` – security hardening baseline
- `docs/security/secret-management.md` – key management and rotation
- `docs/operations/backup.md` – backup/restore procedures
- `docs/operations/admin-bypass.md` – LangGraph checkpoint bypass procedures
- `docs/configuration-reference.md` – all env vars reference
- `docs/system-requirements.md` – hardware and platform requirements
- `docs/upgrade-process.md` – upgrade and rollback procedures

---

## 1. Overview

Self-hosted deployments give the operator full control over the infrastructure
stack, including direct database access, filesystem access, and environment
variable management. This power is necessary for emergency recovery but comes
with significant responsibility – every bypass step documented here **skips
application-layer validation and audit logging**.

### When to Use This Guide vs. File a Support Issue

| Situation | Do this |
|-----------|---------|
| A pipeline run is stuck and `POST /api/v1/runs/{id}/cancel` cannot recover it | Follow `docs/operations/admin-bypass.md` |
| An admin user forgot their password | **Reset via CLI below** (§2) |
| `SECRET_KEY` was rotated and all sessions are invalid | Restart services – sessions re-negotiate (§3.1) |
| `FERNET_KEY` was rotated and credentials fail to decrypt | Re-enter credentials under the new key; `modulo restore --previous-fernet-key` does not recover a lost key (§3.2) |
| Both `SECRET_KEY` and `FERNET_KEY` are lost | **You cannot recover encrypted data** – restore from backup (§4) |
| You cannot authenticate to the admin UI to debug a configuration issue | Local auth bypass (§5) |
| You suspect a bug in the application | File a GitHub issue – do not modify the database |
| You want to add a feature or modify schema | Do not manually DDL – write a migration |
| Data corruption, security incident, or tenant breach | Follow `docs/security/incident-response-playbook.md` |

### Guiding Principle

> **Prefer the application API and CLI commands over direct database access.**
> Only reach for a bypass procedure when the normal path is unreachable (lost
> credentials, corrupt configuration, crashed service).

---

## 2. Admin Password Reset

If the last admin user is locked out (forgotten password, lost SSO provider,
corrupt user record), reset the password via the database:

### 2.1 Identify the Admin User

```sql
SELECT id, email, username
FROM users
WHERE is_admin = true;
```

### 2.2 Generate a bcrypt Password Hash

```bash
# Generate a bcrypt hash using python directly:
python3 -c "
import bcrypt
password = b'<new-temporary-password>'
hashed = bcrypt.hashpw(password, bcrypt.gensalt(rounds=12))
print(hashed.decode())
"
```

### 2.3 Update the Password

```sql
UPDATE users
SET hashed_password = '<bcrypt-hash-from-step-2>',
    password_updated_at = NOW()
WHERE id = '<admin-user-uuid>';
```

### 2.4 Verify

1. Log in via the admin UI or API with the new password.
2. Force a password change on next login if desired.
3. If SSO is the primary auth method, also re-enable the IdP provider in
   admin settings before logging out.

> ⚠️ This bypass does not invalidate existing sessions. If the password reset
> is part of a security incident, rotate `SECRET_KEY` and restart services to
> invalidate all outstanding JWTs (see §3.1).

---

## 3. Key Recovery

### 3.1 Lost `SECRET_KEY`

Rotating `SECRET_KEY` invalidates all existing JWT sessions but does **not**
affect encrypted data at rest (that uses `FERNET_KEY`). Recovery is simple:

1. Generate a new key: `openssl rand -base64 32`
2. Set `SECRET_KEY` in the environment.
3. Restart Modulo services.
4. All users (including admin) must re-authenticate.

**No data is lost.** Sessions are ephemeral – users log back in.

### 3.2 Lost `FERNET_KEY`

**This is a data-loss scenario.** `FERNET_KEY` encrypts connector credentials,
webhook secrets, and model backend API keys at rest. If the key is lost:

1. **Do not restart services** – the application will refuse to start if
   `FERNET_KEY` is absent, but running instances will continue to work with
   cached decrypted credentials in memory.
2. **If the key file was deleted but the process is still running**, recover
   it from `/proc/<pid>/environ` or the process environment if it was
   injected as an env var:
   ```bash
   # Requires root or same-user access
   cat /proc/<modulo-pid>/environ | tr '\0' '\n' | grep FERNET_KEY
   ```
3. **If the key is truly unrecoverable**:
   - Any encrypted credential will fail to decrypt on next access.
   - You must rotate every stored credential (re-create API keys, webhook
     secrets, connector tokens) and re-enter them under the new key via the
     admin UI.
   - Checkpoint blobs encrypted with the old key are **unrecoverable**
     unless you have a backup (see §4).
4. **Always store a backup copy of `FERNET_KEY`** in a separate vault,
   password manager, or offline storage – never only in the running
   environment.

### 3.3 Key Rotation Best Practice

```bash
# Generate new keys while keeping old values
NEW_SECRET_KEY=$(openssl rand -base64 32)
NEW_FERNET_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# Update environment and restart for SECRET_KEY
export SECRET_KEY="$NEW_SECRET_KEY"
# FERNET_KEY rotation requires re-entering stored credentials under the new key
export FERNET_KEY="$NEW_FERNET_KEY"
```

---

## 4. Database Backup & Restore

Full backup/restore procedures are documented in `docs/operations/backup.md`.
This section covers emergency-specific scenarios.

### 4.1 Pre-Bypass Snapshot

Before any write operation against the database outside the application
(startup blocking emergency), take a snapshot:

```bash
pg_dump -Fc -f /tmp/pre-emergency-$(date +%Y%m%dT%H%M%S).dump "$DATABASE_URL"
```

### 4.2 Restore from Snapshot

```bash
# Stop the application
systemctl stop modulo

# Drop and recreate the database
dropdb modulo
createdb modulo

# Restore
pg_restore -d "$DATABASE_URL" /tmp/pre-emergency-*.dump

# Restart
systemctl start modulo

# Verify health
curl -s https://modulo.example.com/healthz | python3 -m json.tool
```

### 4.3 Point-in-Time Recovery

If Postgres WAL archiving is configured, you can recover to a specific
transaction:

```bash
# Restore from base backup, then replay WAL to target time
pg_restore -d modulo /backups/base.dump
cp /var/lib/postgresql/wal_archive/* /var/lib/postgresql/16/main/pg_wal/
pg_ctl promote
```

WAL archiving configuration is **not** set up by default. See
`docs/deployment-security.md` §3.4 for the advisory lock configuration.

---

## 5. Auth Bypass for Debugging

When troubleshooting a configuration issue that prevents login (broken SSO,
corrupt admin user settings, rate-limit self-lockout), you can bypass auth
locally.

### 5.1 Local Access

The `modulo` CLI only exposes `backup` and `restore` (see §4). Emergency
admin actions (password reset, token creation) require direct database
access as described in §2 and below – there is no `modulo users` or
`modulo sessions` command.

### 5.2 Create a Temporary Admin API Token

If you cannot log in via the UI but the application is running, create a
long-lived admin token directly in the database:

```sql
-- Generate a random token (e.g., 64 hex chars)
-- In production, use a proper UUID-based token:
INSERT INTO api_tokens (id, user_id, name, token, scopes, expires_at, created_at, organisation_id)
SELECT
    gen_random_uuid(),
    id,
    'emergency-bypass-token',
    encode(gen_random_bytes(32), 'hex'),
    '["admin:full"]',
    NOW() + INTERVAL '1 hour',
    NOW(),
    organisation_id
FROM users
WHERE is_admin = true
LIMIT 1;
```

Then use the token returned to authenticate API calls:
```bash
curl -H "Authorization: Bearer <token>" https://modulo.example.com/api/v1/admin/settings
```

**Safeguards:**
- Set a short expiry (1 hour in the example above).
- Delete the token after use: `DELETE FROM api_tokens WHERE name = 'emergency-bypass-token';`
- Audit the action in the admin bypass log (see §6).

### 5.3 Rate-Limit Self-Lockout

If rate limiting is blocking your own admin access, wait for the rate-limit
window to expire or restart services to reset in-memory counters. Rate
limits are configured via the admin settings API; there is no
`RATE_LIMIT_DEFAULT` environment variable.

> ⚠️ Do not bypass rate limiting in production without also restricting
> network access (e.g., allowlisting admin IPs at the reverse proxy).

### 5.4 When NOT to Bypass Auth

| Situation | Do NOT | Do this instead |
|-----------|--------|-----------------|
| User reports they cannot log in | Create a DB-level token for them | Reset their password via the procedure in §2 |
| SSO provider is down | Create permanent bypass tokens | Switch to local admin account or wait for SSO recovery |
| You are investigating a security incident | Bypass auth in a way that masks your actions | Use a dedicated audit account, log every command |
| Pipelines are running | Modify user credentials mid-run | Cancel pipelines first, then reset credentials |
| You are not the platform admin | Bypass any auth control | Escalate to the platform owner |

---

## 6. Audit Trail

Every emergency bypass operation **must** be logged to
`/var/log/modulo/admin-bypass.log` (or equivalent for your deployment).
This creates an auditable record independent of the application's
cryptographic audit chain.

### Log Format

Each entry is a single JSON line:

```json
{
  "timestamp": "2026-06-28T12:00:00Z",
  "operator": "admin@example.com",
  "operation": "reset_password | create_api_token | key_rotation | db_restore",
  "reason": "Lost admin access after SSO provider decommission",
  "sql_or_command": "UPDATE users SET hashed_password = ... WHERE id = ...",
  "affected_resources": ["users:<user-uuid>"],
  "backup_taken": true
}
```

### Procedure

```bash
# 1. Take a database backup before any write bypass
pg_dump -Fc -f /tmp/pre-bypass-$(date +%Y%m%dT%H%M%S).dump "$DATABASE_URL"

# 2. Execute the bypass
# 3. Log the operation
cat >> /var/log/modulo/admin-bypass.log <<< '{"timestamp": "...", ...}'

# 4. Verify application still works
curl -s https://modulo.example.com/healthz | python3 -m json.tool

# 5. If the operation caused a regression, restore from backup
pg_restore -d "$DATABASE_URL" /tmp/pre-bypass-*.dump
```

---

## 7. Recovery Decision Tree

```
Emergency situation
  │
  ├─ Can you use the CLI? (uv run modulo ...)
  │     └─ Yes → Use the CLI command
  │     └─ No  → Continue below
  │
  ├─ Is the application running?
  │     └─ Yes → Use temporary API token (§5.2)
  │     └─ No  → Continue below
  │
  ├─ Is it a password/key issue?
  │     └─ Lost SECRET_KEY → Generate new one, restart (§3.1)
  │     └─ Lost FERNET_KEY → Restore from backup (§4) or accept data loss
  │     └─ Forgotten admin password → Reset via DB (§2)
  │
  ├─ Is it a stuck pipeline?
  │     └─ Yes → Follow docs/operations/admin-bypass.md
  │
  └─ Is it a data corruption or suspected bug?
        └─ Data corruption → Restore from backup (§4.3)
        └─ Suspected bug   → File GitHub issue, do not modify data
```

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| LangGraph checkpoint bypass | `docs/operations/admin-bypass.md` |
| Backup & restore | `docs/operations/backup.md` |
| Secret management & rotation | `docs/security/secret-management.md` |
| Incident response | `docs/security/incident-response-playbook.md` |
| Deployment security hardening | `docs/deployment-security.md` |
| Deployment basics | `docs/deployment.md` |
| Admin bypass audit log | `docs/operations/admin-bypass.md` §6 |
| System requirements | `docs/system-requirements.md` |
| Configuration reference | `docs/configuration-reference.md` |
| Upgrade process | `docs/upgrade-process.md` |
| Public launch checklist | `docs/public-launch-checklist.md` |
