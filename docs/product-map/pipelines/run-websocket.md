---
id: feat-pipelines-run-websocket
prd: 6.3
bdd:
  - backend/tests/bdd/features/operations/websocket_reconnection.feature
unit-tests:
  - backend/tests/unit/api/test_websocket_reconnection_bdd.py
  - backend/tests/unit/api/test_ws_token.py
  - backend/tests/unit/auth/test_ws_token.py
code:
  - backend/src/modulo/api/routes/run_ws.py
depends-on:
  - feat-core-pipeline-execution
status: partial
---

# Run WebSocket

Real-time event streaming for pipeline runs over WebSocket. Clients subscribe via a short-lived ws-token, receive live `RunEvent` objects, and can replay buffered events since a sequence number. The connection closes when the run reaches a terminal state.

## Behaviours

- [x] WebSocket connection at `/api/v1/runs/{run_id}/ws`
- [x] Auth via opaque single-use ws-token (preferred) or JWT fallback
- [x] Replay buffered events via `since_event_seq` parameter
- [x] Live event streaming via per-run event broker
- [x] Terminal status detection (complete, failed, cancelled)
- [x] Immediate close if run already terminal at connect
- [x] Clamp excessive replay ranges
- [x] Auth failure closes with code 4001
- [x] Unknown run closes with code 4004
- [x] Missing DB table returns migration error
- [x] DB errors return db_unavailable
- [ ] WS-token rotation and expiry management
- [ ] Cross-process WebSocket broker (currently in-memory only)

## Error Handling

- [x] Auth failure closes WebSocket with code 4001
- [x] Unknown run closes WebSocket with code 4004
- [x] Missing DB table returns migration error message
- [x] DB errors return db_unavailable error
- [x] Immediate close if run already terminal at connect

## Edge Cases

- [x] Run already terminal at connect time — closes immediately
- [x] Excessive replay range (since_event_seq too far back) — clamped to ring buffer limit
- [x] Auth failure (invalid/missing ws-token or JWT) — closes with 4001
- [x] Unknown/non-existent run ID — closes with 4004
- [x] Broker goes away before WebSocket closes — subscriber queue cleaned up via weak references

## Known Gaps

- WS-token rotation and expiry management not implemented (single-use token issued on connect, no refresh mechanism)
- Cross-process WebSocket broker not implemented (in-memory only — works within a single process, breaks under multiple replicas)
- No integration test verifying event streaming across run lifecycle

## QA History

- 2026-07-09: Second-pass product map QA (feat-pipelines-run-websocket): Added Error Handling, Edge Cases, Known Gaps, and QA History sections. Verified all 9 implemented behaviours against run_ws.py code. Confirmed 2 unchecked items (WS-token rotation and cross-process broker) remain genuine unimplemented features.
- 2026-07-11: Third-pass QA: Fixed prd reference (8.1→6.3), populated bdd and unit-tests fields with existing files, removed stale "No BDD feature files" from Known Gaps. Fixed run_ws.py docstring (15 min→60s TTL) and DB error close codes (4004→1011).
