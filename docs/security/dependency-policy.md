# Dependency Update Policy

## Scope

This policy covers all runtime and development dependencies for the Modulo backend (`backend/pyproject.toml` + `backend/uv.lock`) and frontend (`frontend/package.json` + `frontend/pnpm-lock.yaml`).

## CVE Severity Classification

| Severity | CVSS Range | Definition |
|----------|-----------|------------|
| Critical | 9.0–10.0 | Remote code execution, auth bypass, data exfiltration |
| High     | 7.0–8.9   | SSRF, privilege escalation, sensitive data exposure |
| Medium   | 4.0–6.9   | DoS, limited info disclosure, reflected XSS |
| Low      | 0.1–3.9   | Local file read, theoretical attacks, mitigated-by-default |

Severity is taken from the advisory's CVSS score. If no CVSS is available, severity is assessed by the security team based on exploitability and blast radius.

## Response SLAs

| Severity | Fix Deadline | Notification |
|----------|-------------|-------------|
| Critical | 72 hours    | #security-alert immediately |
| High     | 7 days      | #security-alert within 24h |
| Medium   | 30 days     | Tracked in delivery plan |
| Low      | Next release | Triaged on next audit run |

The clock starts when the advisory is published to a trusted source (GitHub Advisory Database, OSV, NVD).

## Scanning Schedule

- **Automated**: `pip-audit` runs in `.github/workflows/ci.yml` on every push to `main` and on each PR to `main`, and again in the deploy workflow (`.github/workflows/deploy.yml`). There is no scheduled weekly dependency scan.
- **Manual**: `uv run pip-audit` (from `backend/`) and `cd frontend && pnpm audit` can be run locally at any time.

## How to Handle a False Positive

1. **Verify**: Confirm the CVE does not apply in Modulo's deployment context (e.g., Windows-only vulnerability on Linux, feature not used).
2. **Document**: Add a row to the approved exceptions table below with the CVE ID, affected package, reason, and reviewer.
3. **Suppress**: Add the CVE ID to `pip-audit`'s `--ignore-vuln` list in the CI workflow. The repo uses `pip-audit` (not the `safety` tool), so there is no `.safety-policy.yml`.

### Approved CVE Override List

| CVE / ID | Package | Reason | Reviewer | Date |
|----------|---------|--------|----------|------|
| *(none)* | | | | |

## Dependency Update Workflow

1. Run `uv run pip-audit` (from `backend/`) and `cd frontend && pnpm audit` before any dependency change.
2. Prefer patch-level updates within the same major version. Major version bumps require a PR with migration notes.
3. Pin transitive dependencies only when they carry a CVE that cannot be resolved by updating the direct dependency.
4. After updating, rerun the full test suite: `uv run pytest` and `cd frontend && pnpm test:unit`.

## Previous Audit Findings

### Backend (June 2026)

| Package | From | To | CVE(s) Fixed |
|---------|------|----|-------------|
| langgraph | 0.2.28 | ≥1.0.10 | PYSEC-2026-83 / CVE-2026-28277 – RCE via msgpack checkpoint deserialization |
| langgraph-checkpoint | 1.0.12 | ≥3.0.0 | CVE-2025-64439 – RCE via jsonplus deserialization |
| langgraph-checkpoint | 1.0.12 | ≥4.0.0 | CVE-2026-27794 – RCE via pickle deserialization |
| langchain-openai | 0.3.35 | ≥1.1.14 | PYSEC-2026-76 / CVE-2026-41488 – SSRF via TOCTOU in image URL fetch |

### Frontend (June 2026)

| Package | From | To | Advisory |
|---------|------|----|----------|
| esbuild | 0.27.7 | ≥0.28.1 | GHSA-g7r4-m6w7-qqqr – Windows dev server arbitrary file read (Low) |
