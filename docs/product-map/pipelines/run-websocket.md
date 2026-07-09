---
id: feat-pipelines-run-websocket
prd: 8.1
code:
  - backend/src/modulo/api/routes/run_ws.py
bdd: []
unit-tests: []
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
