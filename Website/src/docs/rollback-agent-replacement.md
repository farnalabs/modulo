# Rollback Agent Replacement

Revert an agent node back to a manual node using a pipeline snapshot to restore the pre-replacement configuration.

## Overview

When an AI agent node underperforms, you can revert it to a manual (human-performed) node. The revert uses a pipeline snapshot — the same versioned snapshot mechanism used for full pipeline rollback — to restore the node's type, output schema, and configuration from a known-good state.

## API

`POST /api/v1/pipelines/{pipeline_id}/nodes/{node_id}/revert-to-manual?snapshot_id={snapshot_id}`

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `pipeline_id` | UUID | Path | Pipeline containing the node |
| `node_id` | UUID | Path | The agent node to revert |
| `snapshot_id` | UUID | Query | Snapshot containing the manual node configuration |

### Response

Returns the updated pipeline graph (`{ nodes, edges }`).

### Error Codes

| Status | Detail |
|---|---|
| 404 | Pipeline not found / Node not found / Snapshot not found |
| 422 | Only agent nodes can be reverted / Snapshot does not contain this node / Snapshot node was not a manual node / Snapshot node has no output schema |
| 501 | Database migration required (table not found) |

## Related

- [PRD Section 8.4 — Pipeline Builder](/docs/prd#84-pipeline-builder)
- [Product Map — Rollback Agent Replacement](/docs/product-map/core/rollback-agent-replacement)
- `feat-core-rollback-agent-replacement` — Product feature ID
