---
title: GitHub Connector
description: Read/write access to GitHub repositories via the REST API for agent pipelines
---

# GitHub Connector

The GitHub Connector provides authenticated read/write access to GitHub repositories
for use in agent pipelines. It implements `ConnectorBase` and is part of the `git-host`
connector type family.

## Authentication

Authentication is via a Personal Access Token (PAT) passed as `Authorization: Bearer <token>`.
Both classic PATs and fine-grained PATs are accepted, though required scopes differ:

- **Classic PATs**: require `repo` and `read:org` scopes
- **Fine-grained PATs**: require `contents:read`, `contents:write`, `pull_requests:write`

> See the [PRD §8.6](https://modulo.run/docs/prd#8.6) for detailed scope requirements.

## Capabilities

| Resource | Read (`query`) | Write (`write`) |
|---|---|---|
| Repositories | `repos` | — |
| Files | `file` | `file` |
| Pull Requests | `pulls`, `pr_commits`, `pr_files` | `pr`, `pr_comment`, `pr_update` |
| Issues | `issues`, `issue`, `issue_comments`, `issue_events`, `timeline` | `issue`, `issue_update`, `issue_comment`, `issue_label`, `issue_reaction` |
| Labels | `labels` | `label` |
| Milestones | `milestones` | `milestone` |
| Assignees | `assignees` | — |

## Enterprise Features

- **GHES Support**: Configure the API base URL when constructing the connector:
  `GitHubConnector(token, base_url="https://github.internal.example.com/api/v3")`
- **Retry/Backoff**: Automatic retry on 429, 502, 503, 504 with exponential backoff (max 3 retries)
- **Pagination**: List endpoints return a `next_cursor` from the Link header for cursor-based pagination
