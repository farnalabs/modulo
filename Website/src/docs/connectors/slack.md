---
title: Slack Connector
description: Read/write access to Slack workspaces via the Web API for agent pipelines
---

# Slack Connector

The Slack Connector provides authenticated read/write access to Slack workspaces
for use in agent pipelines. It implements `ConnectorBase` and is part of the `chat`
connector type family.

## Authentication

Authentication is via a Slack Bot Token passed as `Authorization: Bearer <token>`.

> See the [PRD §8.6](https://modulo.run/docs/prd#8.6) for detailed requirements.

## Capabilities

| Resource | Read (`query`) | Write (`write`) |
|---|---|---|
| Channels | `channels`, `channel_info`, `channel_members` | — |
| Messages | `messages`, `thread_replies` | `message`, `thread_reply` |
| Users | `users` | — |

## Enterprise Features

- **Retry/Backoff**: Automatic retry on 429 with exponential backoff (max 3 retries)
- **Pagination**: List endpoints return a `next_cursor` for cursor-based pagination
- **Token Validation**: Health check validates token via `api.test` and `auth.test`
