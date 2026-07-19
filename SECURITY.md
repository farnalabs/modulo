# Security Policy

## Overview

Modulo is a self-hosted platform for implementing and continuously improving an
agentic software development lifecycle. We take the security of Modulo and its
users seriously. This document outlines our vulnerability disclosure process
and supported versions.

For detailed guidance on specific security topics, see:

- [Secret Management](docs/security/secret-management.md) — encryption backends,
  key rotation, leaked-secret incident response
- [Input Validation Guide](docs/security/input-validation-guide.md) —
  Pydantic validation conventions, length/range bounds, rejection of raw
  request bodies
- [Dependency Update Policy](docs/security/dependency-policy.md) — CVE
  severity classification, response SLAs, scanning schedule, false positive
  handling

## Reporting a Vulnerability

We encourage responsible disclosure. If you believe you have found a security
vulnerability in Modulo, please report it to us immediately.

**Do not** open a public GitHub issue — use the private channel below.

### Contact

- **Email**: `security@modulo.run`
All reports are acknowledged within 48 hours. We will work with you to
understand the issue, determine its impact, and coordinate a fix.

### What to Include

To help us triage efficiently, please include:

- Type of issue (e.g. RCE, SQL injection, privilege escalation, XSS)
- Affected component(s) and version(s)
- Steps to reproduce (proof of concept preferred)
- Any relevant logs, screenshots, or network traces
- Your name/organisation for credit (optional)

## Disclosure Timeline

We follow a **90-day coordinated disclosure** process:

1. **Report received** — acknowledgement within 48 hours
2. **Triage** — severity assessment, confirmation, and CVE assignment within
   7 days
3. **Fix development** — patch prepared according to the severity SLA
4. **Coordinated release** — patch ships; public disclosure happens after
   90 days or when a fix is available, whichever comes first

The 90-day window gives us time to develop, test, and deploy a fix while
minimising risk to users. If the reporter requests an extension or the fix
requires significant re-architecture, we may agree on an adjusted timeline.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.x (alpha) | Security fixes only |
| < 0.x | No |

During the alpha phase, only the latest pre-release receives security patches.
We recommend always running the most recent version.

## Bug Bounty

Modulo does not currently operate a bug bounty programme. We may introduce one
as the project matures. In the interim, we gratefully acknowledge researchers
in our release notes (with permission).

## Out of Scope

The following are not considered in-scope for security reporting:

- Self-XSS
- Missing HTTP security headers on custom deployments (deployer responsibility)
- Social engineering of Modulo maintainers
- Vulnerabilities in third-party dependencies that are already patched in a
  newer version (upgrade first; report if the patched version has not been
  integrated)
- Theoretical attacks without a practical reproduction

## Safe Harbour

We support coordinated disclosure and will not pursue legal action against
researchers who:

- Follow this policy
- Act in good faith
- Do not access or exfiltrate data beyond what is necessary to demonstrate
  the vulnerability
- Do not disrupt production services

## Attribution

We maintain a `SECURITY.md`-acknowledged list of researchers who have
responsibly disclosed issues (with permission). To be added, let us know
when you report.
