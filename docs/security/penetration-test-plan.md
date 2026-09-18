# Penetration Test Plan: Modulo

| Field | Value |
|---|---|
| Document owner | Security Team |
| Applies to | Modulo (AI SDLC agent governance platform) |
| Classification | Internal (Confidential) |
| Version | 1.0 |
| Last updated | 2026-06-28 |

---

## 1. Purpose & Scope

### 1.1 Purpose

This document defines the methodology, schedule, scope, and reporting framework for penetration testing of the Modulo platform. Testing validates the effectiveness of existing security controls and identifies vulnerabilities before they can be exploited in production.

### 1.2 In Scope

| Component | Details | Target Environment |
|---|---|---|
| **Backend API** | FastAPI REST endpoints (all routes under `/api/v1/`, `/auth/`, `/mcp/`, `/ws/`) | `app.modulo.run` (staging & production) |
| **MCP Server** | JSON-RPC over SSE for agent tool invocation, including tool authorisation, scope enforcement, and input validation | MCP endpoint on `app.modulo.run` |
| **WebSocket Gateway** | Real-time agent session streaming, HITL approval channels, eval result streaming | `wss://app.modulo.run/ws/` |
| **Frontend SPA** | Vue 3 single-page application (all routes), including OIDC redirect flows, session handling, and CSRF token management | `app.modulo.run` |
| **SSO Flows** | OIDC (Google, GitHub, Azure AD) and SAML (Okta, OneLogin): handshake, callback handling, token exchange, session creation | `app.modulo.run/auth/` |
| **CI/CD Pipeline** | Hosted Ubicloud GitHub Actions runner (ubicloud-standard-2); workflow definitions, secret injection, artifact handling | `github.com/farnalabs/modulo` |
| **Container Images** | Docker images for backend, frontend, and supporting services; base image provenance, layer contents, runtime user | Registry: `ghcr.io/farnalabs/*` |
| **Agent Execution Sandbox** | MCP tool execution isolation, prompt injection guards, sensitive data masking, checkpoint encryption | Sandbox Workers on Cloudflare |

### 1.3 Out of Scope

| Area | Rationale |
|---|---|
| Cloudflare infrastructure (WAF, DDoS, CDN, DNS) | Managed by Cloudflare; inherits SOC 2 compliance |
| GitHub infrastructure | Managed by GitHub; inherits SOC 2/ISO 27001 |
| Fly.io control plane | Managed by Fly.io; inherits SOC 2 |
| Physical security | Cloud-hosted; no physical assets under Modulo control |
| End-user endpoints (browsers, OS) | Out of control boundary |
| Third-party SaaS (Linear, Notion, Sentry) | Covered by their respective security postures |
| Social engineering of Modulo staff | Separate engagement if needed |

---

## 2. Schedule

### 2.1 Cadence

Full external penetration tests are conducted **quarterly** (every 3 months). A full test window is 2 weeks.

| Quarter | Window | Status |
|---|---|---|
| Q3 2026 | 2026-07-20 to 2026-08-02 | Upcoming |
| Q4 2026 | 2026-10-19 to 2026-11-01 | Planned |
| Q1 2027 | 2027-01-18 to 2027-01-31 | Planned |
| Q2 2027 | 2027-04-19 to 2027-05-02 | Planned |

### 2.2 Remediation Windows

| Severity | Remediation SLA | Retest Required |
|---|---|---|
| Critical | 72 hours from confirmation | Yes |
| High | 7 calendar days | Yes |
| Medium | 30 calendar days | Yes |
| Low | 90 calendar days | At tester's discretion |
| Info | Next quarterly cycle | No |

### 2.3 Rules of Engagement

- Testing is limited to `app.modulo.run`, `*.modulo.run`, and the Modulo GitHub organisation
- No denial-of-service attacks against production without 48-hour written approval
- No exfiltration of production customer data beyond what is minimally necessary to demonstrate a finding
- All testing must use a dedicated test organisation/tenant with synthetic data
- The testing team must provide a list of source IPs in advance for log correlation
- A communication channel (Signal or Slack) must be established with the on-call engineer before testing begins
- If a critical vulnerability is discovered, testing pauses and the on-call engineer is notified immediately

---

## 3. Methodology

Testing follows the **OWASP Web Security Testing Guide (WSTG) v5.0** with additional coverage for AI-specific attack surfaces and the MCP protocol. Each category maps to WSTG identifiers.

### 3.1 Information Gathering (WSTG-INFO)

- DNS enumeration and subdomain discovery (`modulo.run`, `app.modulo.run`, `demo.modulo.run`)
- Technology fingerprinting (HTTP headers, cookie attributes, framework signatures)
- Endpoint discovery via common API path wordlists (`/api/v1/`, `/mcp/v1/`)
- Directory listing and exposed `.git`, `.env`, and configuration files
- Review of public GitHub repositories for accidental credential exposure
- Archive/cache crawling (Wayback Machine, Google Cache)

### 3.2 Configuration & Deployment Management Testing (WSTG-CONF)

- Review of HTTP security headers (HSTS, CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy)
- CORS misconfiguration testing (origin reflection, wildcard credentials)
- TLS certificate validation, cipher suite strength, HSTS preload status
- Container image analysis: base image freshness, unnecessary packages, non-root user enforcement
- CI/CD pipeline hardening: workflow permissions, secret scoping, artifact exposure
- Environment variable exposure via error messages and debug endpoints

### 3.3 Identity Management Testing (WSTG-IDNT)

- OIDC flow manipulation: state/nonce validation, token replay, CSRF in callback
- SAML assertion manipulation: XML signature wrapping, response tampering, audience restriction bypass
- JWT attacks: alg=none, key confusion (RS256->HS256), weak secret cracking, expired token reuse
- Session fixation during SSO handshake
- Account enumeration via login error messages, password reset timing, and registration responses
- Username/email enumeration via OIDC `id_token_hint` leakage

### 3.4 Authentication & Session Testing (WSTG-ATHN, WSTG-ASVS)

- Credential brute-force resistance (rate limiting, account lockout, CAPTCHA bypass)
- Password policy enforcement (complexity, length, common password rejection)
- Remember-me token security (entropy, rotation, invalidation)
- Session token entropy analysis, cookie attributes (Secure, HttpOnly, SameSite)
- Session fixation and concurrent session handling
- Logout functionality: full session invalidation, token revocation
- Password reset flow: token leakage, timing attacks, email-based reset manipulation
- MFA/2FA bypass testing (if applicable)

### 3.5 Authorization Testing (WSTG-ATHZ)

- **RLS bypass**: Row-Level Security bypass via tenant ID manipulation in path/query/body parameters
- **Team isolation**: Cross-tenant data access via team ID enumeration, project ID guessing
- **Privilege escalation**: Role-based access control bypass (user -> admin, operator -> admin)
- **IDOR**: Insecure direct object reference in all CRUD endpoints (`/projects/{id}`, `/agents/{id}`, `/tasks/{id}`)
- **MCP scope bypass**: Agent attempting to call tools outside its authorised scope
- **HITL bypass**: Human-in-the-loop approval gate bypass via concurrent session manipulation or approval token replay
- **Eval gate bypass**: Evaluation gate circumvention by direct API call or altering eval state

### 3.6 Input Validation Testing (WSTG-INPV)

- SQL injection (all parameters, including JSON body fields and query strings)
- NoSQL injection (if applicable to any MongoDB/Redis interactions)
- Cross-Site Scripting (reflected, stored, DOM-based across all frontend inputs)
- Server-Side Request Forgery (SSRF) via webhook URLs, external integration endpoints, avatar/image fetch
- XML External Entity (XXE) injection in SAML assertion parsing and any XML API
- LDAP injection (if applicable)
- **Prompt injection**: Agent system prompt override via user-supplied context, tool output poisoning, delimiter smuggling
- **Template injection**: Server-Side Template Injection (SSTI) in any rendered output
- Command injection in tool execution parameters
- File upload: path traversal, MIME-type bypass, zip slip, malicious SVG, double extensions
- Open redirect in OIDC/SAML `redirect_uri` and `RelayState` parameters
- HTTP parameter pollution and mass assignment
- GraphQL introspection and query depth attacks (if exposed)

### 3.7 Error Handling Testing (WSTG-ERRH)

- Information leakage via stack traces, debug endpoints, and verbose error messages
- Consistent error response format verification
- Internal IP/hostname disclosure in error messages
- Error-based enumeration (different error for valid vs. invalid resources)
- Custom error page review for stack trace or path disclosure

### 3.8 Cryptography Testing (WSTG-CRYP)

- **Ed25519 signing**: Signature verification bypass, nonce reuse, key extraction
- **Fernet encryption**: Key management, rotation procedure, ciphertext tampering
- **TLS**: Certificate chain validation, weak cipher suites, protocol downgrade (TLS <1.2)
- **Secrets management**: API key storage, encryption at rest, key vault integration
- **Random number generation**: Session tokens, CSRF tokens, nonces: entropy analysis
- **Password storage**: bcrypt/argon2 cost factor verification

### 3.9 Business Logic Testing (WSTG-BUS)

- Workflow bypass: skipping eval steps, approval gates, or compliance checks
- Concurrent session race conditions: double spending of agent credits, duplicate task creation
- State machine manipulation: transitioning eval states out of order
- Resource exhaustion: creating excessive projects, agents, or tasks to exhaust rate limits or storage
- Pricing/billing manipulation (token count tampering, usage meter bypass)
- Webhook replay and idempotency failures

### 3.10 API & MCP-Specific Testing

- **Authentication bypass**: Calling MCP endpoints without a valid session token
- **Tool authorisation bypass**: Invoking MCP tools outside the agent's declared scope
- **SSE validation**: Event stream injection, connection hijacking, message ordering manipulation
- **JSON-RPC validation**: Method enumeration, parameter injection, batch request abuse
- **Rate limiting**: API endpoint throttling bypass via header manipulation or distributed attacks
- **WebSocket security**: Origin validation, message tampering, reconnection hijacking
- **MCP tool fuzzing**: Fuzzing tool parameters with unexpected types, empty values, and boundary conditions

---

## 4. Tools

| Tool | Purpose | License |
|---|---|---|
| **Burp Suite Pro** | Intercepting proxy, automated scanning, Intruder for parameter fuzzing, Repeater for manual testing, Scanner for passive/active crawl | Commercial |
| **OWASP ZAP** | Automated DAST scanning, spidering, active scan rules, API scan mode | Open source |
| **Trivy** | Container image vulnerability scanning, IaC misconfiguration detection, SBOM generation | Open source (Apache 2.0) |
| **gitleaks** | Git repository secret scanning (CI/CD pipelines, public repos) | Open source (MIT) |
| **custom Python scripts** | Tailored fuzzing (param-mining, prompt injection payloads, SSRF probes, JWT manipulation), automated test harness, reporting helper | Internal |
| **Nmap** | Network reconnaissance, service detection, TLS cipher enumeration | Open source |
| **ffuf / dirsearch** | API endpoint and directory discovery with wordlist fuzzing | Open source |
| **JWT_Tool** | JWT attack automation (alg confusion, key cracking, kid injection) | Open source |
| **SQLMap** | Automated SQL injection detection and exploitation (with `--safe-url` for production) | Open source |
| **Kiterunner** | API route discovery with common API path wordlists | Open source |
| **sslscan / testssl.sh** | TLS configuration review | Open source |
| **CSP Evaluator** | Content Security Policy analysis (Google tool) | Free |
| **Semgrep** | SAST rule scanning for custom code patterns | Open source |

---

## 5. Findings Log

All identified vulnerabilities are recorded in the findings log below. The tester populates one row per unique finding.

| ID | Severity | Endpoint | Description | Impact | Remediation | Retest Status | Due Date |
|---|---|---|---|---|---|---|---|
| PT-2026-001 | | | | | | | |
| PT-2026-002 | | | | | | | |
| PT-2026-003 | | | | | | | |
| PT-2026-004 | | | | | | | |
| PT-2026-005 | | | | | | | |

### Severity Definitions

| Severity | Definition |
|---|---|
| **Critical** | Direct, immediate compromise of the platform, data exfiltration, or privilege escalation to super-admin. No user interaction required. |
| **High** | Significant security control bypass, limited data access, or privilege escalation requiring some precondition. |
| **Medium** | Information disclosure, cross-tenant data leakage under limited conditions, or security control weakening. |
| **Low** | Minor information leakage, missing security headers with low exploitation likelihood, or configuration hardening opportunities. |
| **Info** | Informational observation: does not represent a current risk but may be relevant in combination with other findings. |

### Severity Matrix

Severity is assigned based on the intersection of **Impact** and **Likelihood**:

| | Low Impact | Moderate Impact | High Impact |
|---|---|---|---|
| **High Likelihood** | Medium | High | Critical |
| **Moderate Likelihood** | Low | Medium | High |
| **Low Likelihood** | Info | Low | Medium |

---

## 6. Reporting

### 6.1 Deliverables

| Deliverable | Format | Due |
|---|---|---|
| Preliminary findings (raw) | CSV / JSON export from tooling | End of test window |
| Executive summary | PDF + Markdown | 5 business days after test window |
| Technical report | PDF (with appendices for evidence) | 10 business days after test window |
| Remediation tracker | Spreadsheet (Google Sheets) | Updated weekly during remediation |
| Retest report | PDF | 5 business days after retest window |

### 6.2 Report Structure

The technical report must include:

1. **Executive summary**: non-technical overview for management, including risk rating and key metrics
2. **Scope summary**: what was tested, what was excluded, and any deviations from this plan
3. **Methodology**: testing approach with WSTG references
4. **Findings**: one section per finding, ordered by severity (highest first), each containing:
   - Finding ID and severity
   - Endpoint and HTTP method
   - Detailed description and reproduction steps
   - Proof-of-concept (CURL commands, screenshots, or Python scripts)
   - Business and technical impact assessment
   - Remediation recommendation
   - CVSS v3.1 score and vector string
   - References to CWE/CVE where applicable
5. **Retest results**: for any previously identified findings being retested
6. **Appendices**: raw scan outputs, payload lists, full request/response dumps

### 6.3 Remediation Tracking

- All findings are entered into the **Modulo delivery plan** as backlog items with T-shirt size estimates
- Critical and High findings are auto-escalated to the on-call engineer and the CTO
- Remediation progress is tracked weekly in the security review
- Re-testing is scheduled within 5 business days of the remediation deadline
- Findings that cannot be remediated within SLA require a documented risk acceptance signed by the CTO
