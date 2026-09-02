# Break-Glass Admin Recovery Runbook

**Audience:** Modulo operators (SRE / platform / security) who hold the
break-glass operator secrets. This is the operational procedure for
recovering an organisation whose only admin cannot authenticate.

**Prerequisite reading:**
- `docs/prd.md` §7.19 — product requirements for break-glass admin recovery
- `Repos/devtools/adr/017-centralized-authorization.md`, `Repos/devtools/adr/018-centralized-authorization.md` — the auth architecture and the ONE deliberate login-route hook deviation
- `docs/configuration-reference.md` §Break-glass Admin Recovery — env settings
- `docs/deployment-security.md` — deployment security baseline
- `docs/security/secret-management.md` — secret handling / vault

---

## 1. When to use (and when NOT to)

Break-glass recovery exists for ONE situation: the org's last admin cannot
authenticate and no normal recovery path exists.

| Scenario | Use break-glass? |
|---|---|
| Only admin forgot their password | Yes |
| SCIM deactivated / demoted the last admin (no last-admin check fired) | Yes |
| Role demotion locked out the last admin | Yes |
| Lost env secrets (e.g. `SECRET_KEY` / `FERNET_KEY` rotated away) | Yes |
| An active, non-break-glass admin can still log in | **No** — use the normal admin path |
| Password-reset / admin-reactivation flow works | **No** — use the normal flow |
| The outage is transient (network, DB, deploy) rather than an access problem | **No** — fix the outage first |
| The requester cannot be verified out-of-band | **No** — abort; break-glass requires the verification protocol (§5) |
| You merely want a second permanent admin | **No** — break-glass credentials are single-use and time-boxed; provision a real admin instead |

**Don't break glass when a normal admin path exists.** Break-glass is an
emergency, single-use, TTL-bounded credential — it is never a permanent seat
and never a substitute for proper provisioning.

---

## 2. Preconditions

Before an incident, confirm all of the following hold:

1. **Secrets configured** — `MODULO_BREAK_GLASS_ENABLED` is on (or defaults
   from primary/standby secret presence), `MODULO_BREAK_GLASS_SECRET` and
   `MODULO_BREAK_GLASS_STANDBY_SECRET` are set and differ, and
   `MODULO_BREAK_GLASS_DATABASE_URL` is populated. `MODULO_BREAK_GLASS_ENABLED=true`
   with both secrets empty is a startup error; `TTL < 1` or `MAX_TTL < MIN_TTL`
   is a startup error regardless of `ENABLED`.
2. **`modulo_breakglass` role provisioned** — created idempotently by
   `bootstrap_role.py` on every boot (LOGIN, BYPASSRLS, dedicated credential
   from `MODULO_BREAK_GLASS_DATABASE_URL`). Bootstrap failure is FATAL when
   `ENABLED=true`. The role requires a superuser to grant BYPASSRLS — a
   managed-Postgres platform without superuser is a hard platform constraint
   (see ADR-017/018).
3. **CLI installed** — `modulo-break-glass` (registered console script, mirrors
   `modulo-migrate`'s `asyncio.run` wrapper). Connects as `modulo_breakglass`
   via a dedicated lazy engine — never the app `database_url`.
4. **Operator holds the secret** — the operator running the CLI must possess
   `MODULO_BREAK_GLASS_SECRET` / `_STANDBY_SECRET` (actor derived from which
   secret matched) and the DB URL. Secrets live in the KeePassXC vault.

Verify preconditions non-destructively:

```bash
modulo-break-glass smoke
```

Exit 0 = connectivity (`SELECT 1`) + posture assertions (`session_user =
modulo_breakglass`, `deactivate_break_glass` exists) OK. Exit 7 = smoke
failure (see §6). A CLI whose `activate` is disabled (`ENABLED=false`) can
still run `deactivate`, `force-last-admin`, `status`, and `smoke` while
secrets + URL are present.

---

## 3. Recovery procedure (step by step)

1. **Confirm the lockout.** Verify out-of-band that the org admin genuinely
   cannot authenticate and no normal recovery path exists (§1). Open a ticket
   or incident and record its reference — every activation must be traceable
   to it (`--reason` is REQUIRED).

2. **Check existing live rows first:**

   ```bash
   modulo-break-glass status --all
   ```

   If live rows already exist for the org, decide deliberately whether to
   extend/replace them. Multiple live rows for one org are allowed; the
   deactivate step then needs `--account-id` to disambiguate.

3. **Activate:**

   ```bash
   modulo-break-glass activate <org-id|org-slug> \
     --reason "<ticket-ref>" --ttl-minutes 1440
   ```

   - `--reason` REQUIRED (non-empty) — the ticket/incident reference.
   - `--ttl-minutes` default 1440 (24h), min 1, hard cap 4320 (72h) — the 72h
     cap is the compensating bound for the out-of-band protocol.
   - Interactive TTY shells prompt for confirmation; use `--yes` for scripted
     operation. The org is resolved to exactly one org; refusal unless `--yes`
     when not interactive.
   - **Capture the printed credential ONCE.** It is single-use (one-shot) and
     TTL-bounded. It is printed ONLY after the activation transaction commits.
   - If the org is resolved by slug and the CLI warns that a live activation
     already exists, read the warning before proceeding.

4. **Deliver the credential** over a secure channel per the SPECIFIED
   out-of-band verification protocol (§5). Never transmit it through chat/logs
   that are shared or retained.

5. **The org admin logs in** via the normal flow:

   ```
   POST /api/v1/auth/login   (email + password)
   ```

   The login hook consumes the credential with a compare-and-swap: the first
   login succeeds (200); any subsequent login with the same password fails
   (401, byte-identical to a wrong password on a normal account).

6. **After the org admin regains access, tombstone:**

   ```bash
   modulo-break-glass deactivate <org> --reason "<ticket-ref>"
   ```

   This runs the atomic caller-bound `deactivate_break_glass` SECURITY
   DEFINER: token families blacklisted, API keys revoked, memberships
   deactivated, account `active=false`, and for break-glass targets the
   password hash is re-randomized into a tombstone
   (`break_glass_deactivated_at` set, `break_glass_expires_at` cleared). A
   `break_glass_deactivated` audit row is written in the same transaction.

7. **Confirm zero live rows:**

   ```bash
   modulo-break-glass status --all
   ```

   Exit 5 while any live row exists — resolve before closing the incident.
   The runbook mandates `deactivate` of prior live rows unless one is
   deliberately retained.

> **Never leave a live credential outstanding after the incident.** A live
> credential past its verified ticket is a rollback trigger (§10).

---

## 4. force-last-admin

Use `force-last-admin` when the **last non-break-glass admin must be removed**
(e.g. a departed employee) and no replacement exists yet:

```bash
modulo-break-glass force-last-admin <org> --reason "<ticket-ref>"
```

- **This is the ONLY path that removes the last non-break-glass admin.** The
  normal last-admin guard rejects the mutation; force-last-admin overrides it.
- Refuses (exit 5) in a break-glass-only org, when the only live admins are
  break-glass accounts, or when multiple non-break-glass admins exist.
- Same org advisory lock and the same out-of-band verification protocol as
  `activate` (§5).
- Appends a `last_admin_forcibly_removed` audit row in the same transaction.
- The `force` parameter is honoured ONLY for the `modulo_breakglass` session
  (`session_user` gate in the SECURITY DEFINER) — an org admin cannot invoke it.
- **Distinct from `deactivate --force`:** `deactivate --force` overrides the
  refuse-while-live-activation-exists guard; `force-last-admin` overrides the
  last-admin invariant. Semantics are pinned — do not confuse the two.

---

## 5. Credential delivery + the "do you hold the credential?" verification

### TTY vs `--yes`

- **Interactive (TTY):** the CLI prompts for confirmation before activation and
  prints the credential once to the terminal.
- **Scripted (`--yes`):** confirmation is skipped and the credential is written
  once to stdout. Use only when the output channel is secure and not retained.

Either way the credential is printed **once**, as a raw write bypassing the
logger. Copy it into the delivery channel immediately — it cannot be
re-printed.

### The SPECIFIED out-of-band verification protocol

1. **Call OUT** from a pre-registered verified callback number (never accept an
   inbound call as proof of identity). The callback number is stored in the
   vault with an owner.
2. The requester must state a **unique, non-public, single-use ticket reference**
   that only a legitimate requester would know.
3. **Refuse to hand over the credential on first contact** — time-box the
   call-back so there is a deliberate pause before the credential changes
   hands.
4. The operator **independently confirms the org slug/domain** against the
   org's verified billing email on file.

### If the credential print fails after commit (exit 9)

The activation transaction committed but the credential was not displayed —
the credential's value is unknown and must be treated as compromised:

1. `modulo-break-glass deactivate <org> --force --reason "<ticket-ref>"` to
   tombstone the live row.
2. `modulo-break-glass activate <org> --reason "<ticket-ref>"` again to mint a
   fresh credential.
3. Re-verify with `modulo-break-glass status --all`.

The "do you hold the credential?" step is the safety net: before reporting the
incident resolved, confirm the delivered credential is in the org admin's
hands and the live row count is zero after deactivation.

---

## 6. Exit-code table 0-9

| Code | Meaning | Operator action |
|---|---|---|
| `0` | Success | — |
| `1` | Unexpected error | Read the traceback / host log; escalate if it recurs |
| `2` | Usage (missing/empty `--reason`, bad `--account-id`) | Fix the invocation and retry |
| `3` | Org not found (incl. deactivate `M2040` target-does-not-exist) | Verify org id/slug spelling; check the org still exists |
| `4` | Activation-transaction failure (e.g. email-collision retries exhausted) | Retry; if persistent, escalate |
| `5` | Preconditions (force refusals, TTL out of range, the status-sweep live-row exit, operator-auth failure) | Read the message and resolve the specific condition |
| `6` | Deactivate refused (live activation without `--force`, `M2010`/`M2020`) | Add `--force` only if deliberate; confirm scope/account with `--account-id` |
| `7` | Smoke failure (connectivity or posture) | Check `MODULO_BREAK_GLASS_DATABASE_URL` reachability and role provisioning |
| `8` | Deactivate atomicity failure | Investigate; the transaction aborts atomically so no partial state remains |
| `9` | Credential-print failure after commit | Treat credential as unknown → deactivate `--force` then activate again (§5) |

`status --all --json` exits non-zero (`5`) when any live row exists — this is
the daily-sweep signal wired to alerting (§8).

---

## 7. Rotation

- **Standby-to-primary + new standby:** rotate the standby secret into the
  primary slot and mint a new standby. On every rotation run
  `modulo-break-glass status --all` and `deactivate --force` any row not tied
  to a verified ticket.
- **Rotation never invalidates already-delivered credentials.** Credentials are
  bound to the account's `password_hash`, not to the role password — a
  delivered credential keeps logging in and `deactivate`/`status` remain
  operable across a secret rotation. Only NEW `activate` invocations require
  the rotated secret.
- **`MODULO_BREAK_GLASS_DATABASE_URL` also needs rotation** + fresh-DB sync:
  `ALTER ROLE modulo_breakglass ... PASSWORD` (or equivalent), update the vault,
  and run the URL-in-vault-matches-`.env` check.
- **Both-lost recovery:** if primary AND standby secrets are lost, set fresh
  secrets via `fly secrets set`, restart the app, then run
  `modulo-break-glass status --all` and `deactivate --force` every live row.
  A credential already delivered to an org admin remains valid — verify each
  live row against its ticket and deactivate the rest.

---

## 8. Monitoring / detection

Runtime detection is a **daily operator sweep** (the always-on external
consumer is a scheduled follow-up task, tracked in Linear):

- **Executor:** a GitHub Actions scheduled workflow (or a harness scheduled
  task), connecting as `modulo_breakglass` with secrets from the vault.
- **Command:** `modulo-break-glass status --all --json`.
- **Alert:** the non-zero-exit-on-live-rows condition (`5`) is wired to an
  alert. Live rows carry an explicit `reason`, so the alert is actionable.
- **Evidence artifact:** committed to
  `Repos/admin/reviews/break-glass-daily-sweep/YYYY-MM-DD.md`, 90-day
  retention.
- **Forgery detector:** the sweep also verifies the last 24h of audit rows for
  `last_admin_forcibly_removed` / `break_glass_activated` and flags live rows
  without a matching `break_glass_activated` audit — the raw-INSERT forgery
  detector (a forged live row has no matching activation audit).

**A non-zero exit from the sweep means a live break-glass row exists** — treat
it as an incident until it is matched to a verified ticket or deactivated.
Break-glass audit rows are never purged by retention/housekeeping; the
`status --all` sweep and the immutable audit rows together are the (A)
alerting surface, alongside the host-log push for `break_glass_activated` and
`last_admin_forcibly_removed`. Run a quarterly production drill of this
runbook (activate → deliver → login → deactivate) to keep the operator
procedure current.

---

## 9. SCIM 409 IdP-retry note

The SCIM deactivate path returns **409** (RFC 7644 body) when no replacement
admin exists — the IdP must provision one first. The two IdPs behave
differently:

| IdP | Behaviour on 409 | Consequence |
|---|---|---|
| Okta | Stops retrying | Operator must act to resolve |
| Entra (Azure AD) | Retries indefinitely | Repeated 409s until a replacement admin is provisioned |

The IdP-side user stays provisioned/active, **diverging from the DB state**
(where the account is deactivated/tombstoned). This is expected: the DB is
authoritative for access, and the divergence is the IdP's signal that a
replacement is needed.

**Resolution:** provision a replacement admin first — via the IdP for
SCIM-managed orgs, or via the admin API otherwise. A retried SCIM `DELETE` /
`PATCH` on 409 is idempotent (no state churn), so the retries are harmless.

**Log-noise mitigation:** repeated 409s are rate-limited/sampled rather than
logged per retry, and each carries an identical RFC 7644 body so log
processing sees a stable shape. Do not add per-retry logging.

---

## 10. Rollback

- **`deactivate --force` IS the rollback.** It overrides the
  refuse-while-live-activation-exists guard; `--account-id` targets one of
  multiple live rows. The rollback runs the same atomic caller-bound
  `deactivate_break_glass` SECURITY DEFINER, with the
  `break_glass_deactivated` audit in the same transaction. `force-last-admin`
  is distinct and is the only path that removes the last non-break-glass
  admin; a break-glass-only org's lone credential is deactivatable via
  `deactivate --force` (the last-admin check is skipped for break-glass-only
  orgs).
- **Observable rollback triggers:**
  - activation not matched to a verified ticket within N hours;
  - zero logins within the TTL;
  - login from an unexpected actor/IP;
  - `break_glass_probe` events.
- **Deactivate-kills-refresh:** deactivation kills refresh by both mechanisms
  (membership folded to `None` via the SQL-predicate deny + family
  blacklist/API-key revocation) and rejects the original activation password
  after CAS consumption.
- **Re-activation test:** after `deactivate --force`, a fresh `activate`
  produces a new credential that logs in 200; the tombstone persists; two
  `break_glass_activated` audit events exist; a fresh synthesized account is
  created.

---

## 11. Deploy-gate precondition

The break-glass work ships in two deliverables: **(A)** the last-admin
prevention + operator role + migration, and **(B)** the CLI + login-hook
consumption + SQL-predicate deny.

- **Zero live break-glass rows before the (B) deploy.** Run
  `modulo-break-glass status --all` as a one-time ship gate. A non-zero exit
  means a live row exists — resolve it (`deactivate` / `deactivate --force`,
  §3/§10) before deploying (B).
- **Expired rows are deny-covered and MUST NOT block deploys.** A row past
  `break_glass_expires_at` is already denied by the enforcement code itself —
  it is a hygiene item for the daily sweep, not a deploy blocker.

This is a one-time (B)-ship gate, not a recurring precondition. The `alembic
heads == 1` check is a hard (A) deploy-gate failure. From (B) onward, the
daily sweep (§8) is the ongoing monitoring surface.

---

## 12. Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| `smoke` exits `7` | Connectivity or posture failure | Check `MODULO_BREAK_GLASS_DATABASE_URL`, role provisioning (`bootstrap_role.py`), BYPASSRLS grant |
| REST returns `403` | `M2010` — caller not authorized (e.g. `force` param via `modulo_app`) | Use the operator CLI (`modulo_breakglass` session) for operator-only operations |
| REST returns `422` | `M2020` — deactivation would orphan the org (last-admin invariant) | Provision a replacement admin first, or use `force-last-admin` deliberately |
| REST returns `404` | `M2040` — target account does not exist | Verify the `--account-id` / target |
| SCIM returns `409` | `M2010`/`M2020` on the SCIM surface — no replacement admin / caller not authorized | Provision a replacement admin via the IdP (§9); retries are idempotent |
| Login returns `401` | Credential consumed (one-shot CAS), expired, deactivated, or wrong password — byte-identical by design | Mint a fresh credential only if the incident justifies it; otherwise confirm the admin's access was restored (§3) |
| `status --all` exits `5` | At least one live break-glass row | Match each row to a verified ticket or `deactivate --force` (§10) |

**Audit trail location:** org-scoped `AuditEvent` rows with event types
`break_glass_activated`, `break_glass_deactivated`, and
`last_admin_forcibly_removed`, written in the same transaction as the
operation. Audit rows are immutable via the 0005 append-only triggers — every
role (including BYPASSRLS) can INSERT but never UPDATE/DELETE them, so the
host log is the authoritative signal and INSERT-only forgery is the documented
residual. The CLI writes these via the existing Python `append_audit_event` on
the `modulo_breakglass` session (actor in `payload_json`, `account_id` NULL).

---

## Cross-Reference

| Topic | Document |
|---|---|
| Product requirements | `docs/prd.md` §7.19 |
| Auth architecture / login hook | `Repos/devtools/adr/017-centralized-authorization.md`, `Repos/devtools/adr/018-centralized-authorization.md` |
| Env settings | `docs/configuration-reference.md` §Break-glass Admin Recovery |
| Deploy-gate precondition | `docs/deployment.md` §Break-Glass Admin Recovery Deploy Gate |
| Deployment security baseline | `docs/deployment-security.md` |
| Secret handling | `docs/security/secret-management.md` |
| Authoritative plan | `break-glass-admin-recovery-plan.md` (admin repo, v17) |
