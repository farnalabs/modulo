# ADR 027 — Egress centralization: shared-layer OTel metrics, structured reject log, and staged rollout

**Date:** 2026-09-01
**Status:** Accepted (FAR-526D)

---

## Context

FAR-526 centralizes outbound egress by routing every `base_url`-bearing
connector (and the model backends) through the pinned-IP factory in
`core/ssrf.py` (`pinned_async_client` / `pinned_async_client_sync` /
`pinned_async_transport*`), replacing the previous validate-then-connect
pattern. Parts A–C landed the factory, the semgrep `no-raw-httpx-client`
import gate, the connector `_client()` migrations, and the base-seam contract.

Until now, pinning/SSRF rejection was observable **only** for the REST
connector (`modulo_rest_ssrf_blocked_total`, `rest_metrics.record_ssrf_blocked`)
and the two existing warn-logs (`ssrf.invalid_allowlist_entry`,
`ssrf.pinned_transport_proxy_env`). A silent rejection — a pinned transport
refusing a rebound/redirect host (`UnpinnedHostError`), or a resolve failure
now that the pin factory is the single seam for dozens of connectors — had no
shared metric and no structured reject log. The plan-review flagged this
observability gap (and the rollout/rollback question) as a blocker for
centralizing ALL egress behind one path.

## Decision 1 — Shared-layer egress metrics (`core/egress_metrics.py`)

Add a `core/egress_metrics` module mirroring the OTel pattern used by
`core/error_tracking/metrics.py` and `connectors/rest/rest_metrics.py`
(module-level handles, lazy init from `get_meter_provider()`, exception-swallowed
so a metric can never surface as an egress failure), and emit from the shared
egress layer in `core/ssrf.py`:

- **`modulo_egress_pinned_total`** — one per pinned client/transport built;
  labels `host`, `connector_type`.
- **`modulo_egress_rejected_total`** — one per SSRF/egress rejection; labels
  `host`, `connector_type`, `reason`.

`reason` is a stable closed taxonomy: `blocked` (private/internal destination,
malformed host, embedded userinfo, non-canonical IP literal), `unpinned`
(`UnpinnedHostError` — e.g. a redirect hop outside the pin map), `dns-timeout`,
`dns-failed`, `bad-scheme`.

`connector_type` flows as an additive keyword on the factory functions and the
transport/backend classes, defaulting to `unknown`. Existing callers are
unaffected (default); the per-connector labelled rollout (Decision 3) fills in
real values in a follow-up so no Part A–C migration is disturbed.

The pinned counter is emitted where the pin is actually built (the transport
factories); rejections are emitted at the factory boundary (resolve failure) and
in `_PinnedAsyncNetworkBackend.connect_tcp` (`unpinned`). The REST connector's
own `modulo_rest_ssrf_blocked_total` remains — the new layer is additive.

## Decision 2 — Structured reject log

On the reject path, log a structured message carrying `host` + `connector_type`
+ `reason`, in addition to the metric. Today only
`ssrf.invalid_allowlist_entry` / `ssrf.pinned_transport_proxy_env` warnings
exist; the unpinned rejection had no log at all (it raised silently from inside
the transport). A structured log makes a single rejection greppable/diagnosable
without metric aggregation.

## Decision 3 — Staged rollout, canary, and rollback

**Staged rollout.** Per-connector migration (already the shape: Part A–C
migrated connectors one `_client()` at a time). Keep the old per-call-site
validate-then-connect path behind a feature flag for at least one release, so a
connector can be reverted to the pre-pin path without a code change. Do not add
the `connector_type` label wholesale — roll it out connector-by-connector so the
observed metric is attributable.

**Canary.** Deploy to staging first. Assert that `modulo_egress_rejected_total`
for previously-working connectors stays **≈0** (no new rejections on public
egress hosts that used to succeed) and connector health stays `ok:true`. A
spike in `modulo_egress_rejected_total` on a host that "always worked" is the
signal that the pin factory changed behaviour for that host.

**Rollback trigger.** Roll back **that connector's migration** (not the whole
centralization) when **any** migrated connector's health flips `ok:false` with a
`pin-mismatch` / `UnpinnedHostError` on a non-private (PUBLIC) host. A public
host refusing a pin is never an SSRF-policy outcome — it signals the pin
factory (or that connector's pin map) mis-validated a host the connector was
previously reaching. Revert that connector to the old per-call-site path behind
the feature flag; investigate before re-migrating.

## Decision 4 — No stored-credential key rename (read-time-only derivation)

The centralization **does not rename stored credential keys**. The credential
keys a connector reads are derived at READ time from the integration schema's
`credential_fields` map (see `core/library/integrations/definitions.py`); stored
credential rows keep their existing keys in the DB and are never rewritten by
this migration. Changing the schema (e.g. the FAR-515 `app_key` →
`application_key` compat note) makes the connector stop resolving an old key at
connect time — that connector is skipped until re-credentialed — but the stored
row is untouched. **No DB migration renames or rewrites stored credentials.**

## Consequences

- Pinning and SSRF rejection become observable across ALL connectors and model
  backends, not just REST.
- A rejection is greppable (structured log) and countable (metric) by host,
  connector type and reason.
- Rollout risk is bounded: a single connector can be reverted behind the flag,
  staging canary is asserted by the shared metrics, and the rollback trigger is
  an explicit observable signal rather than a guess.
- Existing stored credentials are safe: the schema derivation is read-only; no
  credential row is rewritten. Connectors must be re-credentialed only when a
  schema key changes, which is a separate, explicit compatibility choice.

## References

- Linear FAR-526 (centralization refactor) and FAR-526D (this observability +
  staged rollout).
- ADR 025 — generic REST integration connector (`rest_metrics` precedent).
- FAR-515 compat note — `core/library/integrations/definitions.py`.
- `modulo.core.error_tracking.metrics` — the OTel lazy-init pattern mirrored.
