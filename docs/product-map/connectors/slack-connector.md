---
id: feat-connectors-slack
prd: 8.6
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/slack_connector.feature
unit-tests:
  - backend/tests/unit/connectors/test_slack.py
  - backend/tests/unit/connectors/test_slack_resilience.py
code:
  - backend/src/modulo/connectors/slack/__init__.py
  - backend/src/modulo/connectors/base.py
depends-on:
  - feat-connectors-hub
status: partial
---

# Slack Connector

Async Slack Web API connector implementing `ConnectorBase`. Provides read/write access to Slack workspaces for agent pipelines — reading channels, messages, and users, and posting messages. Authenticated via Slack Bot Token. Belongs to the `chat` connector type.

## Behaviours

### Authentication — Bot Token

- [x] Authenticate all requests via `Authorization: Bearer {bot_token}` header
- [x] Use `httpx.AsyncClient` with base URL `https://slack.com/api`
- [x] `health_check()` calls `api.test` to validate token
- [x] `health_check()` calls `auth.test` to verify token is not revoked
- [x] Return `HealthResult(ok=True)` when `api.test` returns `{"ok": true}`
- [x] Return `HealthResult(ok=False)` with `error` field on failure
- [x] Detect 429 rate-limited responses and surface with Retry-After value
- [x] Automatic 429 retry with exponential backoff (max 3 retries)
- [x] Wrap httpx errors as `ValueError` with descriptive messages
- [x] Detect revoked tokens via `auth.test` and return distinct error

### Channel Operations — listing and reading

- [x] List channels via `query("channels")` with cursor-based pagination
- [x] Call `conversations.list` under the hood
- [x] Return channel id, name, topic, purpose, member count
- [x] Handle cursor-based pagination — returns `next_cursor`
- [x] Filter channels by type (public + private)
- [x] Get channel info (topic, purpose, members) via `query("channel_info")`
- [x] Get channel members via `query("channel_members")`
- [ ] Join channel — not implemented
- [ ] Archive/unarchive channel — not implemented

### Message Operations — reading channel history

- [x] Read messages via `query("messages")` with `channel_id` filter
- [x] Call `conversations.history` under the hood
- [x] Support optional `oldest` and `latest` timestamp filters
- [x] Default to most recent messages when no timestamp filters provided
- [ ] Paginate through full message history — limited to one `conversations.history` call
- [x] Read thread replies via `query("thread_replies")` — calls `conversations.replies`
- [ ] Search messages across channels — not implemented
- [ ] Support message type filtering (messages vs joins vs pins)

### User Operations — workspace user listing

- [x] List users via `query("users")` with cursor-based pagination
- [x] Call `users.list` under the hood
- [x] Return user id, name, display name, real name, email, timezone
- [x] Handle cursor-based pagination — returns `next_cursor`
- [ ] Get user presence status — not implemented
- [ ] Get user profile (including custom fields) — not implemented
- [ ] Lookup user by email — not implemented

### Message Sending — posting to channels

- [x] Post message via `write("message")` with `channel_id` and `text`
- [x] Call `chat.postMessage` under the hood
- [x] Return message timestamp and channel on success
- [x] Support rich message formatting (blocks, attachments) — body_data passes all fields through
- [x] Reply in thread via `write("thread_reply")` — passes `thread_ts` to `chat.postMessage`
- [ ] Support ephemeral messages — not implemented
- [ ] Update message — not implemented
- [ ] Delete message — not implemented
- [ ] Upload file to channel — not implemented
- [ ] Schedule message — not implemented

### Capability Declaration

- [x] `ConnectorType.SLACK` defined in `base.py` enum
- [x] `SlackConnector.connector_type` returns `ConnectorType.SLACK`
- [x] `ConnectorType.SLACK.capabilities` returns `{read, write}` in `base.py`
- [x] Slack capabilities set includes `MESSAGING` — appropriate for a messaging platform

### Health Check — connectivity and credential validation

- [x] Validate Bot Token by calling `api.test` — fail if `ok` is false
- [x] Return error detail from Slack API `error` field on failure
- [x] Return `HealthResult(ok=True)` with no extra detail on success
- [x] Retry 429 with exponential backoff (max 3) before failing
- [x] Detect revoked tokens via `auth.test` and return descriptive error
- [x] Return `HealthResult(ok=False)` for HTTP errors, rate limits, network errors, and API errors
- [ ] Verify bot is in at least one channel (common misconfiguration)

### Error Handling and Resilience

- [x] `_call_api` centralises all HTTP/network error handling with retry/backoff
- [x] `_parse_json` wraps JSON decode errors as `ValueError`
- [x] `verify_scopes()` calls `auth.test` to detect revoked/invalid tokens
- [x] Health check catches all errors and returns `HealthResult(ok=False, detail=...)` — never throws
- [x] Exponential backoff for 429: `base_delay * 2^attempt` with jitter via `Retry-After` header
- [x] Consistent error types — all API errors wrapped as `ValueError` with descriptive message
- [x] `httpx.HTTPStatusError` wrapped as `ValueError("Slack API HTTP {status}: {body}")`
- [x] `httpx.TimeoutException` wrapped as `ValueError("Slack API timeout")` after 3 retries
- [x] `httpx.ConnectError` wrapped as `ValueError("Slack API connection error")` after 3 retries
- [x] `_compute_retry_delay()` helper extracted — retry delay calculation defined once, not repeated 3 times
- [x] `_check_slack_ok()` helper extracted — `ok: false` check defined once, not repeated 8 times
- [x] Health check differentiates network errors from token errors during `verify_scopes` — connection errors, timeouts, and HTTP errors during `auth.test` return "network error" detail instead of misleading "Token is invalid or revoked"
- [x] Empty channel list returns empty `ConnectorResult.records` (not an error)
- [x] Empty message history returns empty `ConnectorResult.records` (not an error)
- [x] Empty user list returns empty `ConnectorResult.records` (not an error)
- [x] `api.test` returning `{"ok": false}` with no `error` field handled — returns `"unknown"` detail
- [ ] Domain-specific exception types (e.g. `SlackRateLimitError`, `SlackAuthError`)

## Known Gaps

- [ ] **No file uploads**: cannot upload files or share files in channels (`files.upload`)
- [ ] **No message search**: `search.messages` API not used; agents cannot search across all channels
- [ ] **Channel history limited**: only one page of `conversations.history` — full history not accessible via pagination
- [ ] **No ephemeral messages**: `chat.postEphemeral` not implemented
- [ ] **No message update/delete**: `chat.update` and `chat.delete` not implemented
- [ ] **No user presence**: `users.getPresence` not implemented
- [ ] **No lookup by email**: `users.lookupByEmail` not implemented
- [ ] **No bot-in-channel verification**: health check does not verify bot is in at least one channel
- [ ] **No domain-specific exception types**: all API and network errors use generic `ValueError` — not domain exceptions like `SlackRateLimitError`, `SlackAuthError`
- [ ] **No scheduling**: `chat.scheduleMessage` not implemented

## QA History
- 2026-07-08: Cross-cutting QA (index 259). Fixed CRITICAL — health check misleadingly reported "Token is invalid or revoked" for network errors (connection errors, timeouts, HTTP errors during `verify_scopes` now return "network error" detail). Fixed MAJOR — extracted `_compute_retry_delay()` helper (retry delay calculation defined once instead of 3 times). Fixed MAJOR — extracted `_check_slack_ok()` helper (8 `if not body.get("ok")` patterns consolidated). Created `test_slack_resilience.py` with 8 new tests covering verify_scopes network error differentiation (3), empty response edge cases (3), `ok:false` no error field (1), connector type constant (1). All 57 existing + 8 new tests pass.
- 2026-07-05: Cross-cutting QA (improve-architecture). Fixed: added retry/backoff for 429 (exponential backoff, max 3 retries); added `_call_api` centralised error handler with ConnectError/TimeoutException/HTTPStatusError coverage; added `_parse_json` for safe JSON decoding; added `verify_scopes()` via `auth.test` to detect revoked tokens; added health check scope verification; added `query("channel_info")`, `query("channel_members")`, `query("thread_replies")`, `write("thread_reply")` resources. Added 57 unit tests (from 30 originally). Updated Known Gaps.
