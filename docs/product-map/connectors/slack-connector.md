---
id: feat-connectors-slack
prd: 8.6
delivery-tasks: []
  - backend/tests/bdd/features/connectors/connector_health.feature
  - backend/tests/bdd/features/connectors/slack_connector.feature
unit-tests: []
code:
  - backend/src/modulo/connectors/slack/__init__.py
  - backend/src/modulo/connectors/base.py

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
- [ ] Support token rotation via ConnectorHub without disrupting in-flight runs
- [ ] Rate-limit awareness — Slack enforces tier-based rate limits (1–50+ per min); no 429 retry/backoff
- [ ] Handle Slack `ResponseMetadata` retry-after for rate-limited responses

### Channel Operations — listing and reading

- [x] List channels via `query("channels")` with cursor-based pagination
- [x] Call `conversations.list` under the hood
- [x] Return channel id, name, topic, purpose, member count
- [x] Handle cursor-based pagination internally — aggregates across pages
- [ ] Filter channels by type (public vs private) — `conversations.list` defaults to public only
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
- [ ] `ConnectorType.SLACK.capabilities` defaults to `frozenset()` — no capabilities assigned in `base.py`
- [ ] `ISSUE_READ`/`ISSUE_WRITE` irrelevant — Slack is not an issue-tracker; a new capability set should be defined (e.g. `CHANNEL_READ`, `MESSAGE_SEND`)
- [ ] Capability-based graph validation — agent requirements vs connector capabilities not yet wired in ConnectorHub

### Health Check — connectivity and credential validation

- [x] Validate Bot Token by calling `api.test` — fail if `ok` is false
- [x] Return error detail from Slack API `error` field on failure
- [x] Return `HealthResult(ok=True)` with no extra detail on success
- [ ] Detect revoked tokens vs network errors vs workspace deactivation
- [ ] Check token scopes during health check (e.g. `channels:history`, `chat:write`)
- [ ] Verify bot is in at least one channel (common misconfiguration)

### Credential Lifetime — ConnectorHub integration

- [ ] Credentials decrypted once at run-start by ConnectorHub — not yet wired
- [ ] Decrypted connector instance held in run-scoped context, never enters LangGraph state
- [ ] One Fernet decrypt call per connector per run — not per node invocation
- [ ] Discard decrypted connector at run end

## Known Gaps

- [ ] **Capabilities not declared**: `ConnectorType.SLACK.capabilities` returns an empty frozenset — no capability model exists for chat-type connectors
- [ ] **No thread support**: cannot read thread replies or reply in threads
- [ ] **No file uploads**: cannot upload files or share files in channels
- [ ] **Text-only messages**: no Block Kit support for rich formatting, buttons, or interactive components
- [ ] **No message search**: `search.messages` API not used; agents cannot search across all channels
- [ ] **Channel history limited**: only one page of `conversations.history` — full history not accessible
- [ ] **BDD placeholder**: `backend/tests/bdd/features/connectors/slack_connector.feature` is a 3-line placeholder with no real scenarios
- [ ] **No unit tests**: `unit-tests` field is empty
- [ ] **No rate-limit handling**: no 429 retry, no `Retry-After` header inspection
- [ ] **No scope verification**: health check does not verify token has required scopes
- [ ] **ConnectorHub pre-run health check not wired**: credentials are not yet decrypted and validated at run-start via ConnectorHub
