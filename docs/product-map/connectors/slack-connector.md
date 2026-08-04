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
- [x] Join channel via `write("channel_join")` — calls `conversations.join` with a `channel` filter
- [x] Leave channel via `write("channel_leave")` — calls `conversations.leave` with a `channel` filter
- [x] Archive/unarchive channel via `write("channel_archive")` / `write("channel_unarchive")` — calls `conversations.archive` / `conversations.unarchive`

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
- [x] Get user presence status via `query("user_presence")` — calls `users.getPresence` with a `user` filter
- [x] Get user profile (including custom fields) via `query("user_profile")` — calls `users.profile.get` with a `user` filter, optional `include_labels`
- [x] Lookup user by email via `query("user_lookup")` — calls `users.lookupByEmail` with an `email` filter

### Message Sending — posting to channels

- [x] Post message via `write("message")` with `channel_id` and `text`
- [x] Call `chat.postMessage` under the hood
- [x] Return message timestamp and channel on success
- [x] Support rich message formatting (blocks, attachments) — body_data passes all fields through
- [x] Reply in thread via `write("thread_reply")` — passes `thread_ts` to `chat.postMessage`
- [x] Support ephemeral messages via `write("ephemeral_message")` — calls `chat.postEphemeral` with `channel`, `user`, and `text` (at least one of `channel`/`user` required)
- [x] Update message via `write("message_update")` — calls `chat.update` with `channel`, `ts`, and updated fields
- [x] Delete message via `write("message_delete")` — calls `chat.delete` with `channel` and `ts`
- [ ] Upload file to channel — not implemented
- [ ] Schedule message — not implemented

### Capability Declaration

- [x] `ConnectorType.SLACK` defined in `base.py` enum
- [x] `SlackConnector.connector_type` returns `ConnectorType.SLACK`
- [x] `ConnectorType.SLACK.capabilities` returns `{MESSAGING, read, write}` in `base.py`
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
- [ ] **No bot-in-channel verification**: health check does not verify bot is in at least one channel
- [ ] **No domain-specific exception types**: all API and network errors use generic `ValueError` — not domain exceptions like `SlackRateLimitError`, `SlackAuthError`
- [ ] **No scheduling**: `chat.scheduleMessage` not implemented

### Resolved (2026-08-04)

- [x] ~~**No ephemeral messages**: `chat.postEphemeral` not implemented~~ — `write("ephemeral_message")` added (`chat.postEphemeral`).
- [x] ~~**No message update/delete**: `chat.update` and `chat.delete` not implemented~~ — `write("message_update")` and `write("message_delete")` added.
- [x] ~~**No user presence**: `users.getPresence` not implemented~~ — `query("user_presence")` added.
- [x] ~~**No lookup by email**: `users.lookupByEmail` not implemented~~ — `query("user_lookup")` added.

## QA History
### 2026-08-04 — improve-architecture: 8 behaviours RESOLVED (user ops, message ops, channel ops)

**RESOLVED 8 behaviours** in the Slack connector (`connectors/slack/__init__.py`):

- **User operations** — `query("user_presence")` (`users.getPresence`, `user` filter → `{user, presence, online}`), `query("user_profile")` (`users.profile.get`, `user` filter + optional `include_labels`, returns full profile incl. custom fields), `query("user_lookup")` (`users.lookupByEmail`, `email` filter → the matching user). All three raise a clear `ValueError` when their required filter is missing.
- **Message operations** — `write("ephemeral_message")` (`chat.postEphemeral`, `channel` + `user` required), `write("message_update")` (`chat.update`, `channel` + `ts` required), `write("message_delete")` (`chat.delete`, `channel` + `ts` required). All pass the remaining payload fields through for rich formatting/blocks.
- **Channel operations** — `write("channel_join")` (`conversations.join`), `write("channel_leave")` (`conversations.leave`), `write("channel_archive")` (`conversations.archive`), `write("channel_unarchive")` (`conversations.unarchive`) — each `channel`-filter required, responses verified via `_check_slack_ok`.

**Tests:** 30 new unit tests in `test_slack.py` (user_presence happy-path/missing-filter/api-error, user_profile happy-path/include_labels/missing-filter/api-error, user_lookup happy-path/missing-email/api-error, ephemeral_message happy/missing-user/api-error, message_update happy/missing-ts/api-error, message_delete happy/missing-ts/api-error, channel_join happy/missing-channel/api-error, channel_leave happy/missing-channel, channel_archive happy/missing-channel/api-error, channel_unarchive happy/missing-channel/api-error) + 10 new BDD scenarios in `slack_connector.feature` (user presence, user profile, user lookup by email, ephemeral message, update message, delete message, join channel, archive channel, unarchive channel, ephemeral-message-without-user error) with 7 new step definitions in `test_connectors.py` and the mock connector extended. Updated product map `connectors/slack-connector.md` (8 behaviours `[ ]`→`[x]`, 4 Known Gaps → RESOLVED, BDD count 18→28, QA History). 99/99 slack unit tests + 28/28 slack BDD scenarios pass (18 pre-existing connector-suite BDD failures unchanged), ruff clean, mypy strict clean. Status: partial (file uploads, message search, channel-history pagination, bot-in-channel verification, domain exceptions, scheduling remain).

- 2026-07-09: Cross-cutting QA (index 345). Fixed CRITICAL — `_RETRYABLE_STATUSES` expanded from `{429}` to `{429, 502, 503, 504}` matching GitHub/Jira connectors (Slack API can return 502/503/504 during transient failures). Fixed MAJOR — removed redundant case-insensitive header check in `_parse_retry_after` (httpx headers already case-insensitive). Fixed MAJOR — corrected product map capabilities entry to `{MESSAGING, read, write}`. Fixed MAJOR — updated BDD step registration comment from "5 scenarios" to "14 scenarios". Updated product map `depends-on:` to include `feat-connectors-base`. Added 4 missing BDD scenarios (channel_info, channel_members, thread_replies, thread_reply) with mock handlers. Added 4 new unit tests for 502/503/504 retry behavior. Created semgrep rule `retryable-5xx-missing` to prevent future connectors from omitting 5xx status codes. Deferred: website docs stub (outside worktree scope), per-request timeout standardization (30s client default is reasonable), client creation pattern (matches GitHub/Jira).
- 2026-07-08: Cross-cutting QA (index 259). Fixed CRITICAL — health check misleadingly reported "Token is invalid or revoked" for network errors (connection errors, timeouts, HTTP errors during `verify_scopes` now return "network error" detail). Fixed MAJOR — extracted `_compute_retry_delay()` helper (retry delay calculation defined once instead of 3 times). Fixed MAJOR — extracted `_check_slack_ok()` helper (8 `if not body.get("ok")` patterns consolidated). Created `test_slack_resilience.py` with 8 new tests covering verify_scopes network error differentiation (3), empty response edge cases (3), `ok:false` no error field (1), connector type constant (1). All 57 existing + 8 new tests pass.
- 2026-07-05: Cross-cutting QA (improve-architecture). Fixed: added retry/backoff for 429 (exponential backoff, max 3 retries); added `_call_api` centralised error handler with ConnectError/TimeoutException/HTTPStatusError coverage; added `_parse_json` for safe JSON decoding; added `verify_scopes()` via `auth.test` to detect revoked tokens; added health check scope verification; added `query("channel_info")`, `query("channel_members")`, `query("thread_replies")`, `write("thread_reply")` resources. Added 57 unit tests (from 30 originally). Updated Known Gaps.
