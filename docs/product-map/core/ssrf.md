---
id: feat-core-ssrf
prd: N/A
adr:
  - Repos/devtools/adr/025-generic-rest-integration-connector.md
code:
  - backend/src/modulo/core/ssrf.py
unit-tests:
  - backend/tests/unit/core/test_ssrf.py
bdd: []
depends-on: []
status: covered
---

# SSRF-safe Outbound URL Validation

The shared SSRF guard for every outbound network hop in Modulo — blocks
private / loopback / link-local / cloud-metadata / CGNAT targets via DNS
resolution, and owns the *pinned-IP* connection transport that closes the
DNS-rebinding window a validate-then-connect pattern leaves open. Consumed by
the SSO test-connection path (`admin_sso.py`), notification delivery
(`notifications.py`), observability test hooks (`observability.py`),
error-forwarder config (``error_forwarder_config.py``), the library-sync client
and the REST connector composition root (ADR 025). Infra-only surface — no UI
route, so it is tracked here rather than in the manifest registry.

## Behaviours

- [x] Strict URL pre-parsing before any resolution: rejects non-http(s)
      schemes, embedded userinfo credentials, missing hostname, and
      odd/out-of-range ports
- [x] Canonical IP literals are validated directly (no DNS); private /
      internal / metadata literal targets fail closed with an actionable
      ValueError
- [x] Non-canonical IP-literal encodings the resolver may reinterpret
      (decimal/hex integers, octal, abbreviated/overlong dotted-numeric,
      scope/percent-encoded zone ids) are rejected before reaching DNS
- [x] A hostname that resolves to **any** blocked address is refused
      (fail-closed across the whole resolved set, IPv4 and IPv6)
- [x] An empty resolution fails closed with a clear error — never a
      usable ``ips[0]`` IndexError crash
- [x] DNS resolution is bounded (``SSRF_DNS_TIMEOUT``, default 10s); a
      hung or failed resolver fails closed rather than stalling the caller
- [x] Hard NON-NEGOTIABLE blocked floor: loopback, link-local, multicast,
      IPv6 site-local, AWS/GCP/Azure link-local metadata, CGNAT,
      benchmarking, current-network and Aliyun metadata ranges are blocked
      **before** any allowlist is consulted
- [x] A configurable private-range allowlist (global
      ``SSRF_ALLOW_PRIVATE_RANGES`` + tenant-scoped ``allow_networks``
      overlay) may permit other private CIDRs, but can never weaken the
      hard floor; invalid entries are logged and skipped instead of failing
      config
- [x] Tenant-scoped ``allow_networks`` layers on top of the global base,
      deduplicated, so a single ``is this address permitted?`` path serves
      every enforcement site
- [x] Sync validation (``validate_outbound_url``, Pydantic validators /
      sync call sites) and async validation
      (``validate_outbound_url_async``, non-blocking ``loop.getaddrinfo``)
- [x] Pinned transport — ``resolve_pinned_ip`` /
      ``pinned_async_transport`` / ``pinned_async_client`` resolve and
      validate once, then force the TCP connection to the validated
      address while keeping the **original** hostname for TLS SNI and
      certificate verification (the URL is never rewritten to the IP)
- [x] Redirects default to not-followed; a redirect hop to an unpinned
      host raises ``UnpinnedHostError`` (fail-closed, never connect to an
      unvalidated destination)
- [x] Host keys are normalised (lowercased, trailing dot stripped) so a
      ``rebind.example.`` host pins against the same key validation
      produced, without changing SNI/cert hostname
- [x] ``trust_env=False`` is the safe default for the pinned transport (a
      trusted proxy would re-resolve the target server-side and defeat
      pinning); explicitly opting into proxy trust with a proxy env var
      present logs a loud warning

## Known Gaps

- **No BDD feature files.** The guard is covered by the dedicated unit
  suite ``backend/tests/unit/core/test_ssrf.py`` (73 test functions incl. the
  DNS-rebinding pinned-transport tests); there are no pytest-bdd features
  for it.
- **Pinning rides a private httpcore seam.** ``_PinnedAsyncNetworkBackend``
  is installed by overriding httpcore's private ``_pool._network_backend``
  attribute; if a future httpcore version stops routing that attribute into
  connection creation, pinning would silently drop — the module makes that
  un-pin loud via a runtime guard, but it is not a public API contract.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — entry added to
  close the feature-graph gap for a shipped infra-only surface whose tests
  and call sites were previously invisible to the product map. Behaviours
  verified against ``backend/src/modulo/core/ssrf.py`` and the 73-test-function
  ``backend/tests/unit/core/test_ssrf.py`` suite; consumers confirmed in
  ``admin_sso.py``, ``notifications.py``, ``observability.py``,
  ``error_forwarder_config.py``, ``library_sync/client.py`` and the ADR 025
  composition root. Status: covered.
