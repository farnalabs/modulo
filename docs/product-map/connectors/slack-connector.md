---
id: feat-connectors-slack
prd: 8.6
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/connectors/slack_connector.feature
unit-tests:
  - backend/tests/unit/connectors/test_slack.py
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
- [x] Return `HealthResult(ok=True)` when `api.test` returns `{"ok": true}`
- [x] Return `HealthResult(ok=False)` with `error` field on failure
- [ ] Slack enforces tier-based rate limits (1–50+ per min); no automatic 429 retry/backoff
- [x] Detect 429 rate-limited responses and surface `Retry-After` value in error
- [x] Raise `HTTPStatusError` on non-2xx HTTP responses from Slack API

### Channel Operations — listing and reading

- [x] List channels via `query("channels")` with cursor-based pagination
- [x] Call `conversations.list` under the hood
- [x] Return channel id, name, topic, purpose, member count
- [x] Handle cursor-based pagination internally — aggregates across pages
- [x] Filter channels by type (public vs private) — `conversations.list` uses both `public_channel` and `private_channel`
- [ ] Get channel info (topic, purpose, members) — not implemented
- [ ] Get channel members — not implemented
- [ ] Join channel — not implemented
- [ ] Archive/unarchive channel — not implemented

### Message Operations — reading channel history

- [x] Read messages via `query("messages")` with `channel_id` filter
- [x] Call `conversations.history` under the hood
- [x] Support optional `oldest` and `latest` timestamp filters
- [x] Default to most recent messages when no timestamp filters provided
- [ ] Paginate through full message history — limited to one `conversations.history` call
- [ ] Read thread replies — not implemented (`conversations.replies`)
- [ ] Search messages across channels — not implemented
- [ ] Support message type filtering (messages vs joins vs pins)

### User Operations — workspace user listing

- [x] List users via `query("users")` with cursor-based pagination
- [x] Call `users.list` under the hood
- [x] Return user id, name, display name, real name, email, timezone
- [x] Handle cursor-based pagination internally — aggregates across pages
- [ ] Get user presence status — not implemented
- [ ] Get user profile (including custom fields) — not implemented
- [ ] Lookup user by email — not implemented

### Message Sending — posting to channels

- [x] Post message via `write("message")` with `channel_id` and `text`
- [x] Call `chat.postMessage` under the hood
- [x] Return message timestamp and channel on success
- [ ] Support rich message formatting (blocks, attachments) — text only
- [ ] Support ephemeral messages — not implemented
- [ ] Update message — not implemented
- [ ] Delete message — not implemented
- [ ] Upload file to channel — not implemented
- [ ] Schedule message — not implemented
- [ ] Reply in thread — not implemented

### Capability Declaration

- [x] `ConnectorType.SLACK` defined in `base.py` enum
- [x] `SlackConnector.connector_type` returns `ConnectorType.SLACK`
- [x] `ConnectorType.SLACK.capabilities` returns `{read, write}` in `base.py`
- [x] Slack capabilities set includes `MESSAGING` — appropriate for a messaging platform

### Health Check — connectivity and credential validation

- [x] Validate Bot Token by calling `api.test` — fail if `ok` is false
- [x] Return error detail from Slack API `error` field on failure
- [x] Return `HealthResult(ok=True)` with no extra detail on success
- [x] Raise `HTTPStatusError` on non-2xx HTTP responses from Slack API
- [x] Detect 429 rate-limited responses and surface `Retry-After` value
- [x] Return `HealthResult(ok=False)` with detail for HTTP errors, rate limits, network errors, and API errors
- [ ] Detect revoked tokens vs network errors vs workspace deactivation
- [ ] Check token scopes during health check (e.g. `channels:history`, `chat:write`)
- [ ] Verify bot is in at least one channel (common misconfiguration)

## Known Gaps

- [ ] **No thread support**: cannot read thread replies or reply in threads
- [ ] **No file uploads**: cannot upload files or share files in channels
- [ ] **Text-only messages**: no Block Kit support for rich formatting, buttons, or interactive components
- [ ] **No message search**: `search.messages` API not used; agents cannot search across all channels
- [ ] **Channel history limited**: only one page of `conversations.history` — full history not accessible
- [ ] **No automatic 429 retry/backoff**: 429 is detected but no automatic retry with exponential backoff
- [ ] **No scope verification**: health check does not verify token has required scopes
- [ ] **No specific exception types**: rate-limit, auth, and API errors all raise generic `ValueError` or `httpx.HTTPStatusError` — not domain-specific exception types

## QA History
- 2026-07-03: Cross-cutting QA: verified "GraphQL Operations" and "Issue Operations" sections already removed from main (previously undocumented fix). Corrected stale connector-hub BDD placeholder claim (was [ ], now [x] — 14 real scenarios exist). Added this QA History section.

