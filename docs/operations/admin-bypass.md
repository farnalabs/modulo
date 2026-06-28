# Admin Bypass: Direct Database Access

**Audience:** Self-hosting platform engineers and SREs with direct Postgres
access. This guide covers emergency procedures for inspecting and modifying
LangGraph checkpoint data at the database level — bypassing the application
layer entirely.

**Prerequisite reading:**
- `docs/deployment-security.md` — deployment security baseline
- `docs/security/secret-management.md` — Fernet key management
- `docs/operations/backup.md` — backup/restore procedures

---

## 1. Overview

Admin bypass is the **last resort** for these scenarios:

| Scenario | When to use |
|----------|-------------|
| **Emergency recovery** | A pipeline run is stuck in an unrecoverable state (e.g., LangGraph runtime bug, corrupt checkpoint blob) and the `/api/runs/:id/reset` endpoint fails |
| **Manual intervention** | A checkpoint must be patched mid-run to unblock an agent loop, or a missing checkpoint must be injected because of a prior crash |
| **Audit investigation** | Tracing agent inputs/outputs across a failed run when application-level logs are insufficient |
| **Key rotation recovery** | Blobs must be re-encrypted after `FERNET_KEY` rotation when the automated `rotate-credentials` command cannot reach all records |

> ⚠️ **Bypass breaks audit chain invariants.** Every procedure in this guide
> manually updates the database without application-level validation. Always
> pair bypass operations with an audit trail entry (see §6).

---

## 2. Checkpoint Schema Reference

Modulo's `ModuloPostgresSaver` extends the upstream LangGraph `PostgresSaver`
by adding `organisation_id` as the first column of every primary key. All
three checkpoint tables live in the `public` schema.

### 2.1 `checkpoints`

Stores the full checkpoint state (JSON) and metadata for each step in a
pipeline run. Each row represents one checkpoint taken during execution.

```sql
CREATE TABLE checkpoints (
    organisation_id   UUID NOT NULL,
    thread_id         TEXT NOT NULL,
    checkpoint_ns     TEXT NOT NULL DEFAULT '',
    checkpoint_id     TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type              TEXT,
    checkpoint        JSONB NOT NULL,       -- encrypted if Fernet is configured
    metadata          JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (organisation_id, thread_id, checkpoint_ns, checkpoint_id)
);

CREATE INDEX ix_checkpoints_org_thread
    ON checkpoints (organisation_id, thread_id, checkpoint_ns);
```

**When encrypted**, the `checkpoint` column contains a JSONB object:
```json
{"__encrypted__": true, "data": "gAAAAA...<fernet-ciphertext>..."}
```

**When plaintext** (legacy or no `FERNET_KEY`), the `checkpoint` column is
standard LangGraph Checkpoint JSON:
```json
{
  "v": 1,
  "ts": "2026-06-28T12:00:00Z",
  "id": "1efabc...",
  "channel_values": { ... },
  "channel_versions": { ... },
  "versions_seen": { ... }
}
```

### 2.2 `checkpoint_blobs`

Stores per-channel serialised state referenced by checkpoints. Each blob is a
byte stream (Python pickle or JSON-serialised value).

```sql
CREATE TABLE checkpoint_blobs (
    organisation_id   UUID NOT NULL,
    thread_id         TEXT NOT NULL,
    checkpoint_ns     TEXT NOT NULL DEFAULT '',
    channel           TEXT NOT NULL,
    version           TEXT NOT NULL,
    type              TEXT NOT NULL,
    blob              BYTEA,
    PRIMARY KEY (organisation_id, thread_id, checkpoint_ns, channel, version)
);

CREATE INDEX ix_checkpoint_blobs_org
    ON checkpoint_blobs (organisation_id, thread_id);
```

Blobs are linked to `checkpoints` rows through the `channel_versions` map in
the checkpoint JSON. Each entry in `channel_versions` maps a channel name to
a version string, and the corresponding blob is found by
`(org, thread, ns, channel, version)`.

**When encrypted**, `blob` contains raw Fernet ciphertext (starts with
`gAAAAA` base64 marker).

### 2.3 `checkpoint_writes`

Stores pending writes (resumed task inputs and pending sends) that have not
yet been consolidated into a checkpoint. These represent in-flight agent
steps and subgraph node outputs.

```sql
CREATE TABLE checkpoint_writes (
    organisation_id   UUID NOT NULL,
    thread_id         TEXT NOT NULL,
    checkpoint_ns     TEXT NOT NULL DEFAULT '',
    checkpoint_id     TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    idx               INTEGER NOT NULL,
    channel           TEXT NOT NULL,
    type              TEXT,
    blob              BYTEA NOT NULL,
    PRIMARY KEY (organisation_id, thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

CREATE INDEX ix_checkpoint_writes_org
    ON checkpoint_writes (organisation_id, thread_id, checkpoint_id);
```

**When encrypted**, `blob` contains raw Fernet ciphertext.

### 2.4 Entity Relationships

```
checkpoints (parent)
  ├── checkpoint_blobs (child, joined via channel_versions in checkpoint JSON)
  │     └── (organisation_id, thread_id, checkpoint_ns, channel, version)
  └── checkpoint_writes (child, joined via checkpoint_id)
        └── (organisation_id, thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
```

Checkpoints form a linked list through `parent_checkpoint_id`:
```
checkpoint_A  ←  checkpoint_B  ←  checkpoint_C  (head = latest)
```

---

## 3. Safe Query Examples

All queries below are **read-only SELECT** and safe to run on a production
database. They assume Postgres 16+ with `psql` connected as the application
role or a read-only replica role.

### 3.1 List all checkpoints for a pipeline run

```sql
-- Replace with the actual organisation and thread IDs
SELECT c.checkpoint_id,
       c.parent_checkpoint_id,
       c.checkpoint_ns,
       c.type,
       c.checkpoint -> '__encrypted__' AS is_encrypted,
       c.metadata ->> 'run_id' AS run_id,
       c.metadata ->> 'step' AS step,
       c.metadata ->> 'agent_name' AS agent_name
FROM checkpoints c
WHERE c.organisation_id = '<org-uuid>'
  AND c.thread_id = '<thread-id>'
ORDER BY c.metadata ->> 'step' ASC;
```

### 3.2 Read encrypted blob content (with decryption)

Blobs in `checkpoint_blobs` and `checkpoint_writes` are encrypted with
Fernet. Use this Python snippet with the same `FERNET_KEY` to decrypt:

```python
"""Decrypt a checkpoint blob for inspection.

Usage:
    python inspect_blob.py <org-uuid> <thread-id> <channel> <version>

Requires: pip install cryptography psycopg[binary]
"""
import json
import sys
import os

from cryptography.fernet import Fernet
import psycopg

FERNET_KEY = os.environ["FERNET_KEY"]  # same as the deployment
DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    org_id, thread_id, channel, version = sys.argv[1:5]

    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """
            SELECT blob, type
            FROM checkpoint_blobs
            WHERE organisation_id = %s
              AND thread_id = %s
              AND checkpoint_ns = ''
              AND channel = %s
              AND version = %s
            """,
            (org_id, thread_id, channel, version),
        ).fetchone()

    if row is None:
        print("No blob found", file=sys.stderr)
        sys.exit(1)

    raw_blob, blob_type = row
    if raw_blob is None:
        print("Blob is NULL")
        return

    is_encrypted = raw_blob[:6] == b"gAAAAA"
    if is_encrypted:
        fernet = Fernet(FERNET_KEY.encode())
        decrypted = fernet.decrypt(raw_blob)
    else:
        decrypted = raw_blob

    value = json.loads(decrypted) if blob_type == "json" else str(decrypted)
    print(json.dumps(value, indent=2, default=str))


if __name__ == "__main__":
    main()
```

### 3.3 Trace agent inputs/outputs for a specific node

Each node invocation in a LangGraph pipeline creates one or more
`checkpoint_writes` rows. The `channel` column indicates which node port
was written (`<node_name>:in`, `<node_name>:out`, or internal channels like
`__pregel_tasks`).

```sql
-- Replace with actual IDs
SELECT cw.task_id,
       cw.idx,
       cw.channel,
       cw.type,
       CASE
           WHEN cw.blob IS NULL THEN NULL
           WHEN encode(substring(cw.blob FROM 1 FOR 6), 'escape') = 'gAAAAA'
               THEN '[ENCRYPTED]'
           ELSE encode(cw.blob, 'escape')
       END AS blob_preview
FROM checkpoint_writes cw
JOIN checkpoints c
    ON  c.organisation_id = cw.organisation_id
    AND c.thread_id       = cw.thread_id
    AND c.checkpoint_ns   = cw.checkpoint_ns
    AND c.checkpoint_id   = cw.checkpoint_id
WHERE cw.organisation_id = '<org-uuid>'
  AND cw.thread_id = '<thread-id>'
  AND cw.checkpoint_ns LIKE '%<node-name>%'
ORDER BY cw.idx ASC;
```

### 3.4 Count orphaned blobs and writes

```sql
-- Orphaned checkpoint_blobs (no matching checkpoint row)
SELECT COUNT(*) AS orphaned_blobs
FROM checkpoint_blobs cb
WHERE NOT EXISTS (
    SELECT 1 FROM checkpoints c
    WHERE c.organisation_id = cb.organisation_id
      AND c.thread_id       = cb.thread_id
      AND c.checkpoint_ns   = cb.checkpoint_ns
);

-- Orphaned checkpoint_writes (no matching checkpoint row)
SELECT COUNT(*) AS orphaned_writes
FROM checkpoint_writes cw
WHERE NOT EXISTS (
    SELECT 1 FROM checkpoints c
    WHERE c.organisation_id = cw.organisation_id
      AND c.thread_id       = cw.thread_id
      AND c.checkpoint_ns   = cw.checkpoint_ns
      AND c.checkpoint_id   = cw.checkpoint_id
);
```

---

## 4. Emergency Bypass Procedures

### 4.1 Update a stuck checkpoint state

If a pipeline run is stuck because the checkpoint metadata or state is
corrupt, you can update the `checkpoint` JSONB directly.

```sql
-- 1. Find the stuck checkpoint
SELECT checkpoint_id, metadata -> 'step' AS step, metadata -> 'status' AS status
FROM checkpoints
WHERE organisation_id = '<org-uuid>'
  AND thread_id = '<thread-id>'
ORDER BY metadata -> 'step' DESC
LIMIT 3;

-- 2. Force the checkpoint metadata to "error" state so the system can proceed
UPDATE checkpoints
SET metadata = metadata || '{"status": "error", "admin_bypass": true, "bypass_timestamp": "2026-06-28T12:00:00Z", "bypass_reason": "corrupt channel_values"}'
WHERE organisation_id = '<org-uuid>'
  AND thread_id = '<thread-id>'
  AND checkpoint_id = '<checkpoint-id>';

-- 3. Verify the update
SELECT checkpoint_id, metadata
FROM checkpoints
WHERE organisation_id = '<org-uuid>'
  AND thread_id = '<thread-id>'
  AND checkpoint_id = '<checkpoint-id>';
```

> ⚠️ Directly modifying checkpoint JSONB may cause LangGraph to fail on
> deserialization. Always update the metadata field rather than the
> checkpoint state JSON unless you fully understand the checkpoint schema.

### 4.2 Manually insert a missing checkpoint

If a checkpoint was never persisted due to a crash but the database
constraints require it (e.g., for a follow-up `aput_writes` call):

```sql
-- 1. Get the encrypted checkpoint JSON from a working backup or known state
--    For plaintext insertion, use a valid Checkpoint dict serialised with json.dumps().
--    For encrypted insertion, encrypt with Fernet first (see §3.2 snippet).

INSERT INTO checkpoints (
    organisation_id, thread_id, checkpoint_ns, checkpoint_id,
    parent_checkpoint_id, checkpoint, metadata
)
VALUES (
    '<org-uuid>',
    '<thread-id>',
    '',                                    -- default checkpoint_ns
    '<new-checkpoint-uuid>',
    '<parent-checkpoint-id>',
    '{"__encrypted__": true, "data": "<fernet-ciphertext>"}',   -- or plain JSON
    '{"admin_bypass": true, "bypass_reason": "inserted missing checkpoint", "bypass_timestamp": "2026-06-28T12:00:00Z"}'
)
ON CONFLICT (organisation_id, thread_id, checkpoint_ns, checkpoint_id)
DO NOTHING;  -- skip if already present

-- 2. Verify insertion
SELECT checkpoint_id, metadata
FROM checkpoints
WHERE organisation_id = '<org-uuid>'
  AND thread_id = '<thread-id>';
```

> ⚠️ Inserting a checkpoint with the wrong `parent_checkpoint_id` breaks the
> checkpoint chain. The pipeline runtime expects a linked list from the
> latest checkpoint back to the first. Verify the chain with §3.1 before
> inserting.

### 4.3 Clean up orphaned checkpoint data

Orphaned blobs and writes accumulate when a run is force-deleted or crashes
mid-write. They waste space and slow down queries.

```sql
-- 1. Identify orphans (preview only — do not delete yet)
SELECT 'checkpoint_blobs' AS table, count(*)
FROM checkpoint_blobs cb
WHERE NOT EXISTS (
    SELECT 1 FROM checkpoints c
    WHERE c.organisation_id = cb.organisation_id
      AND c.thread_id       = cb.thread_id
      AND c.checkpoint_ns   = cb.checkpoint_ns
)

UNION ALL

SELECT 'checkpoint_writes' AS table, count(*)
FROM checkpoint_writes cw
WHERE NOT EXISTS (
    SELECT 1 FROM checkpoints c
    WHERE c.organisation_id = cw.organisation_id
      AND c.thread_id       = cw.thread_id
      AND c.checkpoint_ns   = cw.checkpoint_ns
      AND c.checkpoint_id   = cw.checkpoint_id
);

-- 2. Delete orphans (run within a transaction for safety)
BEGIN;

DELETE FROM checkpoint_blobs cb
WHERE NOT EXISTS (
    SELECT 1 FROM checkpoints c
    WHERE c.organisation_id = cb.organisation_id
      AND c.thread_id       = cb.thread_id
      AND c.checkpoint_ns   = cb.checkpoint_ns
);

DELETE FROM checkpoint_writes cw
WHERE NOT EXISTS (
    SELECT 1 FROM checkpoints c
    WHERE c.organisation_id = cw.organisation_id
      AND c.thread_id       = cw.thread_id
      AND c.checkpoint_ns   = cw.checkpoint_ns
      AND c.checkpoint_id   = cw.checkpoint_id
);

COMMIT;

-- 3. Verify counts dropped to zero
-- (re-run the SELECT from step 1)
```

### 4.4 Re-encrypt blobs after `FERNET_KEY` rotation

If the automated `rotate-credentials` command cannot reach all rows (e.g.,
after a partial migration), re-encrypt in bulk:

```python
"""Re-encrypt all checkpoint blobs under a new Fernet key.

Usage:
    FERNET_KEY_OLD=<key> FERNET_KEY_NEW=<key> DATABASE_URL=<url> uv run reencrypt_blobs.py
"""
import os
import psycopg
from cryptography.fernet import Fernet, InvalidToken

OLD_KEY = os.environ["FERNET_KEY_OLD"]
NEW_KEY = os.environ["FERNET_KEY_NEW"]
DATABASE_URL = os.environ["DATABASE_URL"]

fernet_old = Fernet(OLD_KEY.encode())
fernet_new = Fernet(NEW_KEY.encode())


def reencrypt_blob(blob: bytes | None) -> bytes | None:
    if blob is None or blob[:6] != b"gAAAAA":
        return blob  # not encrypted, skip
    try:
        decrypted = fernet_old.decrypt(blob)
    except InvalidToken:
        # Already under the new key — skip
        return blob
    return fernet_new.encrypt(decrypted)


def main():
    with psycopg.connect(DATABASE_URL) as conn:
        # Re-encrypt checkpoint_blobs
        rows = conn.execute(
            "SELECT organisation_id, thread_id, checkpoint_ns, channel, version, blob "
            "FROM checkpoint_blobs WHERE blob IS NOT NULL"
        ).fetchall()
        for row in rows:
            org_id, thread_id, ns, channel, version, blob = row
            new_blob = reencrypt_blob(blob)
            if new_blob is not blob:
                conn.execute(
                    "UPDATE checkpoint_blobs SET blob = %s "
                    "WHERE organisation_id = %s AND thread_id = %s "
                    "AND checkpoint_ns = %s AND channel = %s AND version = %s",
                    (new_blob, org_id, thread_id, ns, channel, version),
                )

        # Re-encrypt checkpoint_writes
        rows = conn.execute(
            "SELECT organisation_id, thread_id, checkpoint_ns, checkpoint_id, "
            "       task_id, idx, blob "
            "FROM checkpoint_writes"
        ).fetchall()
        for row in rows:
            org_id, thread_id, ns, ckpt_id, task_id, idx, blob = row
            new_blob = reencrypt_blob(blob)
            if new_blob is not blob:
                conn.execute(
                    "UPDATE checkpoint_writes SET blob = %s "
                    "WHERE organisation_id = %s AND thread_id = %s "
                    "AND checkpoint_ns = %s AND checkpoint_id = %s "
                    "AND task_id = %s AND idx = %s",
                    (new_blob, org_id, thread_id, ns, ckpt_id, task_id, idx),
                )

        conn.commit()

    print(f"Re-encrypted {sum(1 for _ in ())} blobs")  # simplified


if __name__ == "__main__":
    main()
```

---

## 5. Safety Warnings

### 5.1 Bypass breaks audit chain invariants

The application-level audit log uses cryptographic chaining — each entry
includes the SHA-256 hash of the previous entry. Direct database writes
create no audit entries. After a bypass operation:

- The `audit verify` command will **not** detect the bypass (it validates
  the audit log chain, not the checkpoint tables).
- There is no record of who changed what unless you manually create one
  (see §6).
- Rollback is dependent on your own transaction logging — the application
  does not version checkpoint rows.

### 5.2 Orphaned data risk

- Writing a checkpoint row without the corresponding blobs leaves the
  checkpoint in an unreadable state — LangGraph will fail on `aget_tuple`.
- Deleting a checkpoint row does not cascade to `checkpoint_blobs` or
  `checkpoint_writes` — you must clean them separately (§4.3).
- Modifying `parent_checkpoint_id` breaks the linear checkpoint chain and
  may cause the runtime to fail on traversal.

### 5.3 Encryption mismatch risk

- If you insert or update a `checkpoint` JSONB value, it must match the
  encryption state expected by the application. If `FERNET_KEY` is
  configured, the `checkpoint` field must be `{"__encrypted__": true,
  "data": "<ciphertext>"}` or LangGraph will fail on decrypt.
- The reverse is also true: if `FERNET_KEY` is not configured, inserting an
  encrypted JSON wrapper will cause a JSON decode error at the application
  level.

### 5.4 When NOT to bypass

| Situation | Do NOT | Do this instead |
|-----------|--------|-----------------|
| Run is still active | Manually modify checkpoint state | Cancel the run via API first |
| You are unsure of the org ID | Guess or omit `organisation_id` | Check the `auth.organisation` or `runs` table |
| Migration is in progress | Write to checkpoint tables | Wait for migration to complete |
| A LangGraph update is pending | Modify schema structure | Apply the update first, then reassess |

---

## 6. Audit Trail

Every bypass operation **must** be recorded in a file at
`/var/log/modulo/admin-bypass.log` (or equivalent for your deployment).
This creates an auditable record independent of the application's
cryptographic audit chain.

### Log format

Each entry is a single JSON line:

```json
{
  "timestamp": "2026-06-28T12:00:00Z",
  "operator": "admin@example.com",
  "operation": "update_checkpoint | insert_checkpoint | delete_orphans | reencrypt_blobs",
  "reason": "Corrupt channel_values blocking pipeline run abc-123",
  "sql": "UPDATE checkpoints SET metadata = ... WHERE ...",
  "affected_tables": ["checkpoints"],
  "affected_threads": ["<thread-id>"],
  "affected_orgs": ["<org-uuid>"],
  "backup_taken": true
}
```

### Procedure

```bash
# 1. Take a database backup before any bypass operation
pg_dump -Fc -f /tmp/pre-bypass-$(date +%Y%m%dT%H%M%S).dump "$DATABASE_URL"

# 2. Execute the bypass (see §4)
# 3. Log the operation
cat >> /var/log/modulo/admin-bypass.log <<< '{"timestamp": "...", ...}'

# 4. Verify application still works
curl -s https://modulo.example.com/api/v1/health | python3 -m json.tool

# 5. If the operation caused a regression, restore from backup
pg_restore -d "$DATABASE_URL" /tmp/pre-bypass-*.dump
```

### Mandatory backup policy

| Bypass type | Backup required? | Retention |
|-------------|------------------|-----------|
| SELECT / read-only queries | No | N/A |
| UPDATE on `checkpoints.metadata` | Yes | 90 days |
| INSERT into `checkpoints` | Yes | 90 days |
| DELETE from `checkpoint_blobs` / `checkpoint_writes` | Yes | 90 days |
| Bulk re-encrypt of blobs | Yes | 180 days |
| DDL (ALTER TABLE, DROP) | **Never** — escalate to engineering | N/A |

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| Fernet key rotation | `docs/security/secret-management.md` §How to Rotate Secrets |
| Checkpoint encryption architecture | `docs/security/checkpoint-encryption.md` |
| Backup and restore | `docs/operations/backup.md` |
| Audit log chain verification | `docs/deployment-security.md` §6.1 |
| Deployment security hardening | `docs/deployment-security.md` |
| Incident response playbook | `docs/security/incident-response-playbook.md` |
