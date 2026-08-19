# Security Policy

Modulo is a self-hosted agent governance platform for building governed,
repeatable AI-assisted software delivery pipelines. We take the security of Modulo and its
users seriously. This document outlines our vulnerability disclosure process
and supported versions.

## Reporting a Vulnerability

We encourage responsible disclosure. If you believe you have found a security
vulnerability in Modulo, please report it to us immediately.

**Do not** open a public GitHub issue.

### Contact

**Email**: `security@modulo.run`

Prefer GitHub's built-in private reporting when possible: on the repository
page, use **Report a vulnerability** (a private security advisory) — this is the preferred
channel and avoids sending sensitive material over email, which is not
end-to-end encrypted.

For email reports, avoid including production credentials, tokens, or other
live secrets; a proof of concept that does not expose data is sufficient.

Automated security scan output (e.g. pip-audit, npm audit, Trivy) is accepted
as a report.

All reports are acknowledged within 48 hours. We will work with you to
understand the issue, determine its impact, and coordinate a fix.

### What to Include

To help us triage efficiently, please include:

- Type of issue (e.g. RCE, SQL injection, privilege escalation, XSS)
- Affected component(s) and version(s)
- Steps to reproduce (proof of concept preferred)
- Any relevant logs, screenshots, or network traces
- Your name/organisation for credit (optional)

### Out of Scope

The following are not considered in-scope for security reporting:

- Self-XSS
- Missing HTTP security headers on custom deployments (deployer responsibility)
- Social engineering of Modulo maintainers
- Vulnerabilities in third-party dependencies that are already patched in a
  newer version (upgrade first; report if the patched version has not been
  integrated)
- Theoretical attacks without a practical reproduction

## Disclosure Timeline

We follow a **90-day coordinated disclosure** process:

1. **Report received** — acknowledgement within 48 hours
2. **Triage** — severity assessment and confirmation within 7 days
3. **Fix development** — patch prepared according to the severity SLA (see
   [Dependency Update Policy](docs/security/dependency-policy.md))
4. **Coordinated release** — patch ships; public disclosure happens after
   90 days or when a fix is available, whichever comes first

CVE assignment is arranged through GitHub Security Advisories for confirmed,
CVE-worthy issues; if an issue does not meet CVE criteria we will confirm that
decision explicitly rather than leaving the report unacknowledged.

The reporter is given advance notice before any public disclosure, and either
party may request an extension to the 90-day window.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest 0.x (alpha) | Security fixes only |
| Earlier 0.x | No — upgrade to the latest |

During the alpha phase, only the latest pre-release receives security patches;
earlier 0.x releases receive no patches, so please upgrade before reporting. We
recommend always running the most recent version. Even during alpha, security
bugs are treated as high priority and are not held for a full release cycle.

## Bug Bounty

Modulo does not currently operate a bug bounty programme. We may introduce one
as the project matures. In the interim, we gratefully acknowledge researchers
in our release notes (with permission).

## Safe Harbour

We support coordinated disclosure and will not pursue legal action against
researchers who:

- Follow this policy
- Act in good faith
- Do not access or exfiltrate data beyond what is necessary to demonstrate
  the vulnerability
- Do not disrupt production services

## Attribution

Researchers are acknowledged in the release notes of the release that fixes
their report, with permission. To be added, let us know when you report.

## Further Reading

- [Secret Management](docs/security/secret-management.md) — encryption backends,
  key rotation, leaked-secret incident response
- [Input Validation Guide](docs/security/input-validation-guide.md) — Pydantic
  validation conventions, length/range bounds, rejection of raw request bodies
- [Dependency Update Policy](docs/security/dependency-policy.md) — CVE severity
  classification, response SLAs, scanning schedule, false positive handling
- [Penetration Test Plan](docs/security/penetration-test-plan.md) — scope,
  schedule, and process
- [Incident Response Playbook](docs/security/incident-response-playbook.md) —
  detection, containment, and recovery procedures
