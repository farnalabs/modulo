# Incident Response Playbook

**Audience:** Platform engineers, SREs, security team, and on-call responders
for Modulo production deployments.

**Prerequisite reading:**
- `docs/deployment-security.md` – deployment hardening, audit log, rate limiting
- `docs/security/secret-management.md` – key rotation, credential leak response
- `docs/security/dependency-policy.md` – CVE response SLAs
- `docs/security/input-validation-guide.md` – prompt injection & validation
- `docs/operations/backup.md` – backup/restore procedures

---

## 1. Severity Classification

### 1.1 Five-Tier Scale

| Severity | Definition | Blast Radius | Examples |
|----------|------------|-------------|----------|
| **Critical** | Active exploit causing data loss, multi-tenant breach, or complete service unavailability | Whole platform or all tenants | Data exfiltration, SSO/OIDC compromise, RLS bypass exploited in production, database compromise, audit chain tampering |
| **High** | Exploitable vulnerability with confirmed impact, significant service degradation, or single-tenant data exposure | Single tenant or service component | API key leak (production), container escape, prompt injection with data egress, encryption key compromise, rate limit bypass causing resource exhaustion |
| **Medium** | Exploitable in limited scenarios, no confirmed data access, or non-critical service degradation | Single user or edge case | Reflected XSS (no auth token exposure), low-severity info leak via error messages, CSRF on non-critical endpoint, dependency with High CVE not yet exploited |
| **Low** | Theoretical risk, mitigated-by-default, or informational finding with no current exploit path | None (defense-in-depth) | Missing security header, verbose error in dev/staging only, dependency with Medium/Low CVE, log containing internal IPs |
| **Info** | Best-practice recommendation, non-security finding, or documentation gap | None | Weak cipher available but not negotiated, missing rate limit on non-critical endpoint, outdated docs |

### 1.2 CVSS Mapping

| Modulo Severity | CVSSv3 Range |
|----------------|-------------|
| Critical | 9.0–10.0 |
| High | 7.0–8.9 |
| Medium | 4.0–6.9 |
| Low | 0.1–3.9 |
| Info | 0.0 |

### 1.3 Severity Factors

When assessing severity, consider:

1. **Data sensitivity** – Does the incident expose checkpoint data, credentials, PII, or agent logs?
2. **Multi-tenant impact** – Is the blast radius contained to one org or crosses tenant boundaries?
3. **Exploit complexity** – Is there a known PoC? Is authentication required?
4. **Detection source** – Internal monitoring, user report, or external disclosure (e.g., HackerOne)?
5. **Persistence** – Is the compromise active (ongoing data egress) or historical (logs found after the fact)?

---

## 2. Escalation Paths

### 2.1 Contact Channels

| Channel | Purpose | Used For |
|---------|---------|----------|
| `#security-alert` (Slack) | Automated alerts from monitoring + initial human triage chat | All severities |
| `#security-on-call` (Slack) | Dedicated incident channel (created per incident) | Critical, High |
| PagerDuty | Phone/SMS push notification for on-call engineer | Critical, High |
| `security@modulo.run` | Email archive, compliance trail | All severities (CC on closure) |
| Signal / phone tree | Out-of-band contact when Slack is down | Critical only |

### 2.2 Response SLAs

| Severity | First Response | Triage Complete | Mitigation Target | Status Update Cadence |
|----------|---------------|----------------|-------------------|----------------------|
| Critical | < 30 min | < 1 h | < 4 h | Every 30 min |
| High | < 2 h | < 4 h | < 24 h | Every 2 h |
| Medium | < 8 h (business hours) | < 24 h | < 72 h | Daily |
| Low | < 2 business days | < 5 business days | Next release | Weekly |
| Info | Next sprint planning | Triage at planning | Future sprint | Per sprint |

### 2.3 Escalation Tree

```
Critical / High incident detected
  │
  ├─► Tier 1 (on-call engineer)
  │     Respond within SLA
  │     Ack in PagerDuty or #security-alert
  │     Create #security-on-call-YYYYMMDD Slack channel
  │     Begin triage
  │     If unresolved after 30 min (Critical) / 1 h (High):
  │       │
  │       └─► Tier 2 (security lead / principal engineer)
  │             Join incident channel
  │             Coordinate containment
  │             If unresolved after 1 h (Critical) / 4 h (High):
  │               │
  │               └─► Tier 3 (CTO / head of engineering)
  │                     Business-impact decisions
  │                     External comms approval
  │                     Legal / compliance notification
  │
Medium / Low incident detected
  │
  ├─► Security team member assigned during business hours
  │     Verify severity
  │     Create ticket in delivery plan
  │     Schedule fix per SLA
```

### 2.4 On-Call Responsibilities

- **Primary on-call:** First responder for all incidents. Carries the pager.
  Rotation: weekly, Mon 09:00 UTC.
- **Secondary on-call:** Backup if primary does not ack within 10 min. Same
  rotation, offset by 1 week.
- **Security lead:** Tier 2 escalation. Not on pager rotation – available
  during business hours + call-out for Critical.
- **CTO / head of engineering:** Tier 3 escalation. Authorises public
  disclosure, legal notifications, and business-continuity decisions.

### 2.5 Out-of-Hours Protocol

| Severity | Action |
|----------|--------|
| Critical | Page primary on-call immediately. If no ack in 10 min, escalate to secondary. |
| High | Page primary on-call. If no ack in 20 min, escalate to secondary. |
| Medium | Log ticket. Assign next business day. |
| Low | Log ticket. Triage at next planning. |

---

## 3. Incident Response Phases

### 3.1 Preparation

**Always-in-place readiness:**

- [ ] On-call rota published and current
- [ ] PagerDuty integration tested monthly (simulated Critical alert)
- [ ] Monitoring dashboards cover all alert signals from `docs/deployment-security.md` §6.4
- [ ] Audit log chain integrity verified weekly: call `GET /api/v1/admin/audit/verify` (requires the `audit.manage` permission; there is no CLI wrapper)
- [ ] Backup encryption passphrase accessible to on-call (separate from prod secrets)
- [ ] Access to cloud provider console, deployment hosts, and DB read-replica documented in `docs/operations/break-glass-admin-recovery-runbook.md`
- [ ] Incident channel Slack workflow tested quarterly
- [ ] Pentest findings remediated per `docs/security/penetration-test-plan.md`
- [ ] Dependency scans pass per `docs/security/dependency-policy.md`

### 3.2 Detection & Analysis

**Signals to monitor** (from `docs/deployment-security.md` §6.4):

| Signal | Source | Possible Incident |
|--------|--------|-------------------|
| Audit chain integrity failure | `GET /api/v1/admin/audit/verify` | Log tampering, DB compromise |
| Failed login rate spike (>10/min per IP) | Backend logs / OTel | Credential stuffing, SSO probe |
| 429 rate limit threshold crossed | Reverse proxy logs | DoS, brute force |
| Unexpected outbound connection | Network policy logs | Data exfiltration, C2 beacon |
| Container with Critical CVE deployed | Trivy scan CI | Known-exploit path |
| CSRF token mismatch spike | Backend error logs | CSRF probing, automated scanning |
| Unexplained 403/401 without auth header | Backend access logs | API key leak, token reuse |
| RLS policy violation logged | Postgres logs | Multi-tenant cross-read |

**Triage steps:**

1. **Acknowledge** the alert in PagerDuty or `#security-alert`.
2. **Create** incident channel `#security-on-call-YYYYMMDD`.
3. **Classify** severity (see §1). If unsure, classify up.
4. **Snapshot** current state:
   - `docker compose -f deploy/compose/docker-compose.prod.yml ps`
   - `docker compose -f deploy/compose/docker-compose.prod.yml logs modulo --tail=200`
   - `GET /api/v1/admin/audit/verify` (audit chain integrity)
   - `GET /healthz/ready` (readiness)
5. **Preserve** evidence before any remediation:
   - Export affected container logs: `docker compose -f deploy/compose/docker-compose.prod.yml logs modulo > incident-YYYYMMDD-container.log`
   - Snapshot audit log window: `GET /api/v1/admin/audit/export?page=... > incident-YYYYMMDD-audit.jsonl` (audit viewer feature; paginate to cover the window)
   - Capture Postgres connection state: `SELECT * FROM pg_stat_activity;`
   - If container compromise suspected: `docker compose -f deploy/compose/docker-compose.prod.yml exec modulo cat /proc/1/cmdline`
6. **Determine scope:**
   - What tenants/users are affected?
   - What data was accessed?
   - Is the attack active or historical?
   - Is there evidence of lateral movement?

### 3.3 Containment

| Severity | Containment Action | Trade-off |
|----------|-------------------|-----------|
| Critical | **Immediate isolation** – revoke compromised creds, block egress IPs, scale affected service to zero | Full or partial service downtime |
| High | **Targeted mitigation** – rate-limit affected endpoint, revoke individual tokens, block suspicious IPs | Feature degradation for some users |
| Medium | **Patch deployment** – hotfix through normal CI, no infrastructure change | No immediate action beyond fix |
| Low | **Ticket** – fix in next regular sprint | None |

**Containment procedures by incident type:**

**SSO/OIDC compromise:**
1. Disable the compromised IdP provider in Modulo admin settings.
2. For each affected org user, deactivate via `POST /api/v1/admin/users/{user_id}/deactivate`, which blacklists their JWT token families and revokes their API keys.
3. Notify org admins of IdP config re-validation (see §8.1 for template).
4. Rotate `OIDC_CLIENT_SECRET` for the affected provider.

**API key leak:**
1. Revoke the leaked key in the admin UI or via `DELETE /api/v1/api-keys/{key_id}`.
2. Rotate the affected service's secrets in the deployment environment (e.g. `SECRET_KEY`, connector credentials) and redeploy; there is no rotation CLI.
3. Check audit log for API calls made with the leaked key in the exposure window.
4. If the key belongs to a third-party connector, rotate the connector's credentials.

**RLS bypass:**
1. Confirm which table(s) and org(s) are affected via Postgres logs.
2. Apply emergency RLS policy if missing:
   ```sql
   DROP POLICY IF EXISTS org_isolation_emergency ON <table>;
   CREATE POLICY org_isolation_emergency ON <table> FOR ALL USING (organisation_id = current_setting('session_modulo.org_id')::uuid);
   ```
3. Block API access to the affected endpoint until RLS is verified.
4. Notify affected orgs per data-breach procedure.

**Prompt injection attack:**
1. Identify the injected pipeline ID and checkpoint.
2. Stop the affected pipeline's execution by cancelling active runs (run cancel via the UI/MCP `cancel_run` tool); there is no pipeline-freeze CLI.
3. Review checkpoint data for exfiltration attempts.
4. Update the prompt guard rules (see `docs/security/input-validation-guide.md`).

**Container vulnerability exploit:**
1. Confirm the CVE and affected image digest.
2. Block inbound traffic to affected containers at the firewall / network policy.
3. Deploy patched image with the fix (see `docs/security/dependency-policy.md`).
4. Scan for indicators of compromise in the affected container's filesystem and network logs.

**Data exfiltration (detected):**
1. Block the destination IP/domain at the network egress firewall.
2. Revoke all active sessions for the affected user(s).
3. Capture a snapshot of recent egress logs.
4. Determine the data scope (tables accessed, rows exported, checkpoint blobs).

**DoS/DDoS:**
1. Enable WAF rate limiting at the reverse proxy.
2. Enable `SYN flood` protection if available at the provider.
3. Scale affected service horizontally.
4. If attack persists, switch to Cloudflare Magic Transit or equivalent DDoS mitigation.

**Insider threat:**
1. Suspend the user's account: `POST /api/v1/admin/users/{user_id}/deactivate`.
2. Revoke all active sessions for the user: deactivation blacklists the user's JWT token families and revokes their API keys in one step.
3. Export audit log for the user's recent activity.
4. Preserve all evidence for HR/legal review.

### 3.4 Eradication

1. **Remove root cause:**
   - Deploy patched code (hotfix branch → gate → merge through normal CI).
   - Rotate all secrets exposed during the incident.
   - Apply missing security controls (RLS policy, rate limit, input guard).
2. **Verify eradication:**
   - Run affected test suite: `uv run pytest backend/tests/ -k <related> -v`.
   - Run security-specific tests: `uv run pytest backend/tests/integration/ -k "rls or audit" -v` (RLS/audit coverage lives in `backend/tests/integration/`, e.g. `test_api_key_audit_rls.py`, `test_audit_append_only.py`).
   - Confirm audit chain integrity: `GET /api/v1/admin/audit/verify`.
   - Confirm no residual access: test with revoked credentials.
3. **Scan for secondary compromise:**
   - Check for cronjobs, scheduled tasks, or webhooks added during the window.
   - Review IaC state for unapproved changes: `terraform plan`.
   - Verify container image digests match CI-signed builds.

### 3.5 Recovery

1. **Restore service:**
   - Scale services back to normal replica count.
   - Re-enable any disabled endpoints or features.
2. **Verify normal operation:**
   - `GET /healthz/ready` returns ready/green.
   - Smoke-test the affected feature through the UI.
   - Confirm monitoring alerts are back to baseline.
3. **Re-enable elevated protections:**
   - Remove any emergency rate limits or IP blocks added during containment.
   - Notify affected users that service is restored.

### 3.6 Post-Mortem

See §6 for the full process. Minimum steps:

1. Write the incident timeline.
2. Identify root cause and contributing factors.
3. Define action items with owners and deadlines.
4. Add lessons learned to the appropriate product map entry.
5. Update this playbook if any procedure was insufficient.

---

## 4. Common Incident Types

### 4.1 SSO/OIDC Compromise

| Attribute | Details |
|-----------|---------|
| Severity | **Critical** |
| Detection | Failed login spike, unexpected IdP admin user added, session replay from unknown IPs |
| Containment | Disable IdP provider, revoke all sessions |
| Eradication | Rotate `OIDC_CLIENT_SECRET`, validate IdP config |
| Prevention | Enforce IdP-initiated SSO validation per `docs/product-map/auth/sso-provider-ui.md` |

### 4.2 API Key Leak

| Attribute | Details |
|-----------|---------|
| Severity | **High** (production), **Medium** (staging) |
| Detection | gitleaks CI pass failure, unexpected API calls from unknown IPs, GitHub secret scan alert |
| Containment | Revoke leaked key, rotate connector credentials |
| Eradication | Remove key from git history (BFG + force push), verify no copies in logs |
| Prevention | Pre-commit hook with gitleaks, API key masking in all outputs |

### 4.3 RLS Bypass

| Attribute | Details |
|-----------|---------|
| Severity | **Critical** (if exploited), **High** (if discovered internally) |
| Detection | Postgres logs showing cross-org queries, audit log anomalies, pentest finding |
| Containment | Emergency RLS policy, block affected endpoint |
| Eradication | Add missing policy, review all tables for RLS coverage |
| Prevention | RLS guarantee tests in CI (`backend/tests/integration/`), quarterly RLS audit |

### 4.4 Prompt Injection Attack

| Attribute | Details |
|-----------|---------|
| Severity | **High** (data egress), **Medium** (prompt manipulation without egress) |
| Detection | Agent returning unexpected output, checkpoint blob with injected instructions, alert from output guard |
| Containment | Freeze pipeline, isolate agent session |
| Eradication | Update prompt guard rules, patch input sanitizer |
| Prevention | Layered guards (input + output), per `docs/security/input-validation-guide.md` |

### 4.5 Container Vulnerability Exploit

| Attribute | Details |
|-----------|---------|
| Severity | **High** (with known PoC), **Medium** (theoretical) |
| Detection | Trivy scan in CI/CD, GitHub Advisory alert, security scanner notification |
| Containment | Block network to affected Pod, deploy patched image |
| Eradication | Verify no IOCs, rebuild image from pinned base |
| Prevention | Weekly dependency scans, pinned base image digests |

### 4.6 Data Exfiltration

| Attribute | Details |
|-----------|---------|
| Severity | **Critical** |
| Detection | Unexpected outbound connection alert, audit log with bulk export, unusual data volume in network metrics |
| Containment | Block destination IP, revoke sessions, freeze pipelines |
| Eradication | Rotate all secrets accessed in window, restore data if corrupted |
| Prevention | Egress network policies, data-loss prevention on bulk exports, checkpoint encryption |

### 4.7 DoS/DDoS

| Attribute | Details |
|-----------|---------|
| Severity | **High** (service degradation), **Critical** (complete unavailability) |
| Detection | 429 rate limit threshold crossed, latency spikes, error rate increase, auto-scaling triggered |
| Containment | WAF rate limiting, horizontal scale-up, DDoS mitigation provider |
| Eradication | Block attack source IPs, update rate limit rules |
| Prevention | Redis-backed token bucket rate limits, WAF rules, CDN shielding |

### 4.8 Insider Threat

| Attribute | Details |
|-----------|---------|
| Severity | High |
| Detection | Unusual access patterns, bulk data download, audit log with off-hours activity |
| Containment | Suspend account, revoke sessions, preserve evidence |
| Eradication | Rotate secrets the user had access to, review IaC changes |
| Prevention | Least-privilege RLS, audit chaining, session recording for sensitive operations |

---

## 5. Communication Templates

### 5.1 Escalation Message (Slack)

```
🚨 *INCIDENT ESCALATION – #<incident-id>*
Severity: <Critical | High>
Type: <SSO compromise | API key leak | RLS bypass | prompt injection | ...>
Detected: <timestamp UTC>
Affected: <org(s) / user(s) / service(s)>
Current status: <triage in progress | containment active | mitigated>

Responders: @on-call-primary @on-call-secondary
Channel: #security-on-call-YYYYMMDD

Initial context:
<2–3 sentence summary of what happened, how detected, and blast radius>
```

### 5.2 Incident Channel Pinned Summary

```
📌 *INCIDENT #<incident-id>*

Severity: <Critical | High | Medium>
Status: <detecting | containing | eradicating | recovering | closed>
Opened: <timestamp UTC>
Closed: <timestamp UTC | –>

Lead: @handle
Responders: @handle1, @handle2

Timeline:
- T+0: Alert received
- T+0:05: Channel created
- T+<n>: <key event>

Links:
- PagerDuty incident: <url>
- Post-mortem doc: <url>
- Commits: <url1>, <url2>
```

### 5.3 Status Update (Slack)

```
*Status Update – <incident-id> – <time since detection>*

What happened: <1-sentence summary>
Current action: <containing | patching | monitoring | ...>
Impact: <users affected / services degraded / data at risk>
Next update: <time>

Responders: @on-call-primary
```

### 5.4 Resolution Announcement (Slack)

```
✅ *INCIDENT RESOLVED – #<incident-id>*

Type: <incident type>
Duration: <Xh Ym>
Root cause: <1–2 sentence summary>
Action taken: <what was deployed / rotated / blocked>
Data impact: <none | <n> checkpoint blobs re-encrypted | <n> sessions revoked>
Post-mortem: #<link> (due within 5 business days)
```

### 5.5 Customer Notification (Email / Admin UI Banner)

```
Subject: Security Incident Notification – <incident-id>

Dear <org admin>,

Modulo has identified and responded to a security incident affecting
<org name>. The incident occurred between <start> and <end> UTC.

Impact summary:
- <what data was accessed, if any>
- <what actions were taken>
- <what you need to do, if anything>

We have:
- <containment action 1>
- <eradication action 2>
- <recovery action 3>

A detailed post-mortem will be available at <link> within 5 business days.

If you have questions, contact security@modulo.run.
```

### 5.6 Legal / Compliance Notification (Email)

```
Subject: [CONFIDENTIAL] Security Incident Notification – <incident-id>

To: <DPO / legal counsel / compliance officer>

This is a formal notification of a security incident affecting Modulo.

Incident ID: <id>
Detection date: <date>
Severity: <Critical | High>
Type: <type>
Affected entities: <orgs / users / data classifications>
Regulatory jurisdiction: <GDPR / SOC2 / HIPAA / ...>

A full incident report will follow within 72 hours.
For immediate questions, contact security@modulo.run.
```

---

## 6. Post-Mortem Process

### 6.1 When a Post-Mortem Is Required

| Severity | Post-Mortem Required | Deadline | Reviewer |
|----------|---------------------|----------|----------|
| Critical | Yes | 5 business days | Security lead + CTO |
| High | Yes | 10 business days | Security lead |
| Medium | Recommended | Next sprint | Assigned engineer |
| Low | Optional | Per team discretion | – |

### 6.2 Timeline Format

All post-mortems must include a chronological timeline in this format:

```
| Time (UTC) | Event | Evidence |
|------------|-------|----------|
| 2026-06-28 12:00 | Alert triggered: audit chain failure | PagerDuty incident #123 |
| 2026-06-28 12:03 | On-call acked | Slack #security-alert |
| 2026-06-28 12:05 | Incident channel created | #security-on-call-20260628 |
| 2026-06-28 12:12 | Triage: RLS policy missing on `checkpoints` table | `docker compose logs` output |
| 2026-06-28 12:30 | Containment: emergency RLS policy applied | Git commit <sha> |
| 2026-06-28 13:00 | Eradication: full RLS audit and fix deployed | Git commit <sha> |
| 2026-06-28 13:15 | Recovery: service restored | `GET /healthz/ready` |
| 2026-06-28 13:30 | Incident closed | Slack #security-on-call-20260628 |
```

### 6.3 Root Cause Analysis

Structure the analysis around these questions:

- **What happened?** Narrative of the incident from detection to closure.
- **Why did it happen?** Technical root cause (code bug, config gap, missing control).
- **Why wasn't it caught earlier?** Detection gap analysis.
- **What was the blast radius?** Actual vs. potential impact.
- **What went well?** Procedures that worked as intended.
- **What went wrong?** Gaps in detection, response, or communication.

### 6.4 Action Items

Each action item must include:

| Field | Required | Example |
|-------|----------|---------|
| ID | Yes | `PM-2026-06-28-001` |
| Description | Yes | Add RLS policy to `checkpoints` table |
| Owner | Yes | @engineer-name |
| Severity | Yes | Critical |
| Due date | Yes | 2026-07-05 |
| Verification | Yes | `pytest tests/unit/test_rls.py -v` passes |
| Linked ticket | Recommended | Task ID from delivery plan |

### 6.5 Lessons-Learned Integration

After the post-mortem is approved:

1. **Add to product map** – If the incident reveals an undocumented edge case or
   missing behaviour, add it to the relevant product map entry under "Known Gaps"
   or as a new behaviour checkbox.
2. **Update AGENTS.md** – If the incident pattern is likely to recur (e.g.,
   missing RLS on a new table), add a prevention rule to `Development/Product/AGENTS.md`
   or the most specific subdirectory AGENTS.md.
3. **Update this playbook** – If the response procedure was missing a step or
   a containment action was insufficient, revise the relevant section.
4. **Schedule a tabletop exercise** – For Critical incidents, schedule a
   tabletop exercise within 30 days to validate that the new controls work.
5. **Update monitoring** – If detection was delayed or missed entirely, add
   or adjust the alert rule per `docs/deployment-security.md` §6.4.

### 6.6 Post-Mortem Template

```markdown
# Post-Mortem: <incident-id>

## Incident Summary

<3–5 sentence executive summary>

## Severity

<Critical | High | Medium>

## Timeline

| Time (UTC) | Event | Evidence |
|------------|-------|----------|

## Root Cause Analysis

### What happened?
### Why did it happen?
### Why wasn't it caught earlier?
### Blast radius
### What went well
### What went wrong

## Action Items

| ID | Description | Owner | Severity | Due | Verification |
|----|-------------|-------|----------|-----|-------------|

## Lessons Learned

<Paragraph on how this informs future development, testing, or operations.>

## Cross-References

- Incident channel: #security-on-call-YYYYMMDD
- Related commits: <sha>
- Updated docs: <paths>
```

---

## 7. Incident Classification Quick Reference

| Incident Type | Default Severity | First Action | SLA Clock |
|--------------|-----------------|-------------|-----------|
| SSO/OIDC compromise | Critical | Disable IdP provider | Immediate |
| API key leak (prod) | High | Revoke key | < 2 h |
| API key leak (staging) | Medium | Revoke key | < 8 h |
| RLS bypass (exploited) | Critical | Emergency RLS policy | Immediate |
| RLS bypass (discovered) | High | Add policy + audit | < 2 h |
| Prompt injection (egress) | High | Freeze pipeline | < 2 h |
| Prompt injection (no egress) | Medium | Update guard rules | < 8 h |
| Container CVE (Critical w/ PoC) | High | Block Pod network | < 2 h |
| Container CVE (High, no PoC) | Medium | Schedule patch | < 8 h |
| Data exfiltration | Critical | Block destination IP | Immediate |
| DoS/DDoS | High | Enable WAF rate limiting | < 2 h |
| Insider threat | High | Suspend account | < 2 h |
| Audit chain integrity failure | Critical | DB snapshot + investigation | Immediate |
| Secret exposed in log | Medium | Rotate secret + scrub logs | < 8 h |
| Weak TLS cipher (TLS 1.0/1.1) | Low | Update nginx config | Next release |

---

## 8. Appendices

### 8.1 IdP Re-Validation Request (for SSO compromise)

```
Subject: Action Required – Re-validate SSO Configuration

Dear <org admin>,

As part of our response to a security incident, we have disabled
the SSO provider configuration for <org name>. Before we can
re-enable it, please:

1. Verify that the OIDC client credentials in your IdP are current.
2. Rotate the client secret in your IdP console.
3. Confirm the allowed callback URLs include only:
   https://<modulo-instance>/auth/callback

Once confirmed, contact security@modulo.run to re-enable SSO.
```

### 8.2 Key Rotation Procedure (Post-Leak)

```
1. Rotate SECRET_KEY:      Set a new value in the deployment environment and redeploy
2. Rotate FERNET_KEY:       Set a new base64-encoded 32-byte value in the deployment environment and redeploy
3. Rotate DATABASE_URL:     Update in deployment environment + redeploy
4. Rotate OIDC_CLIENT_SECRET: Update in IdP console + Modulo admin settings
5. Invalidate all sessions: deactivate and reactivate affected users via `POST /api/v1/admin/users/{user_id}/deactivate` (blacklists token families); there is no session-revoke CLI
```

### 8.3 Evidence Preservation Checklist

- [ ] Container logs exported: `docker compose -f deploy/compose/docker-compose.prod.yml logs modulo > incident-YYYYMMDD-container.log`
- [ ] Audit log window exported: `GET /api/v1/admin/audit/export` (paginated; there is no audit CLI)
- [ ] Database snapshot: `pg_dump -Fc modulo > incident-YYYYMMDD.dump`
- [ ] Container image digest recorded: `docker image inspect ghcr.io/farnalabs/modulo:${TAG} | jq '.[0].RepoDigests'` (the production compose service uses image `ghcr.io/farnalabs/modulo:${TAG}`)
- [ ] Network logs captured from cloud provider (VPC flow logs, host firewall logs)
- [ ] Active users/accounts snapshot: query the accounts via the admin API (`GET /api/v1/admin/users`); there is no session CLI
- [ ] Checkpoint state snapshot: list runs via the UI or `GET /api/v1/runs`

### 8.4 Testing the Playbook

Conduct a tabletop exercise quarterly:

1. Pick a scenario from §4.
2. Walk through detection → containment → eradication → recovery steps verbally.
3. Verify that:
   - On-call contact info is current.
   - Slack channels exist and are accessible.
   - Runbook commands produce expected output.
   - Evidence preservation procedure is clear.
4. Document gaps and update this playbook.

### Cross-Reference

| Topic | Document |
|-------|----------|
| Deployment security hardening | `docs/deployment-security.md` |
| Secret management | `docs/security/secret-management.md` |
| Dependency policy (CVE SLAs) | `docs/security/dependency-policy.md` |
| Input validation / prompt guards | `docs/security/input-validation-guide.md` |
| Penetration test plan | `docs/security/penetration-test-plan.md` |
| Backup & restore | `docs/operations/backup.md` |
| Self-hosted admin operations | `docs/operations/self-hosted-admin.md` |
| Network egress audit | `docs/operations/network-egress.md` |
| Product map (behaviour tracking) | `docs/product-map/` |
