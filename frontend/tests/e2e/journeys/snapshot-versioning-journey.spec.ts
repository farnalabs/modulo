import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  cleanupJourneyEntities,
  createManualNodePipeline,
  setPipelineGraph,
  uniqueName,
  type JourneyCleanup,
  type PipelineRef,
} from '../setup/realstack-api'

/**
 * Real-stack snapshot-versioning journey (FAR-1242 batch 2).
 *
 * Runs against the REAL backend (staging/app); skips the local target. Two
 * live-edit snapshots of the same pipeline are saved, one is tagged, and the
 * pair is diffed — the persisted version chain and its structural diff, the
 * state rollback is a pointer swap into.
 *
 * The live-edit chain is feature-gated (pipeline_diff_rollback): when the
 * feature is absent on the target the journey skips loudly instead of failing.
 */

interface SnapshotResponse {
  id: string
  snapshot_version: number
  tag: string | null
  version_kind: string
}

test.describe('Real-stack journeys: snapshot versioning', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('two edits produce a versioned snapshot chain that tags and diffs structurally', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const cleanup: JourneyCleanup = { pipelineIds: [], schemaIds: [], token, apiBase }
    const created = await createManualNodePipeline(
      apiBase,
      token,
      uniqueName('E2E Journey Snapshots'),
      uniqueName('E2E Snapshot Schema'),
    )
    cleanup.pipelineIds.push(created.pipeline.id)
    cleanup.schemaIds.push(created.schemaId)
    const pipeline: PipelineRef = created.pipeline
    try {
      await loginAsAdmin(page, env)

      // Edit 1: save a live-edit snapshot of the single-node graph.
      const save1 = await apiFetch<SnapshotResponse>(apiBase, token, 'POST', `/api/v1/pipelines/${pipeline.id}/snapshots`, {})
      if (save1.status === 402 || save1.status === 404) {
        test.skip(true, `live-edit snapshots are feature-gated and absent on this target (HTTP ${save1.status})`)
        return
      }
      expect(save1.status).toBe(200)
      const snapshotA = save1.body
      expect(snapshotA?.version_kind).toBe('edit')
      expect(snapshotA?.id).toBeTruthy()

      // Edit 2: add a second manual node, then snapshot again.
      const secondNodeId = crypto.randomUUID()
      await setPipelineGraph(apiBase, token, pipeline.id, [
        {
          id: created.nodeId,
          node_type: 'manual',
          label: 'E2E Human Input',
          position: { x: 120, y: 120 },
          output_schema_id: created.schemaId,
        },
        {
          id: secondNodeId,
          node_type: 'manual',
          label: 'E2E Human Input 2',
          position: { x: 320, y: 220 },
          output_schema_id: created.schemaId,
        },
      ])
      const save2 = await apiFetch<SnapshotResponse>(apiBase, token, 'POST', `/api/v1/pipelines/${pipeline.id}/snapshots`, {})
      expect(save2.status).toBe(200)
      const snapshotB = save2.body
      expect(snapshotB?.id).toBeTruthy()
      expect(snapshotB?.snapshot_version).toBeGreaterThan(snapshotA?.snapshot_version ?? 0)

      // Persisted chain: both live-edit versions are listed.
      const listRes = await apiFetch<{ items: SnapshotResponse[]; total: number }>(
        apiBase,
        token,
        'GET',
        `/api/v1/pipelines/${pipeline.id}/snapshots?page_size=50`,
      )
      expect(listRes.status).toBe(200)
      const listedIds = new Set(listRes.body?.items.map((s) => s.id) ?? [])
      expect(listedIds.has(snapshotA?.id ?? '')).toBe(true)
      expect(listedIds.has(snapshotB?.id ?? '')).toBe(true)

      // Tag the earlier version; a re-list reads the persisted tag back.
      const tagRes = await apiFetch<SnapshotResponse>(
        apiBase,
        token,
        'PATCH',
        `/api/v1/pipelines/${pipeline.id}/snapshots/${snapshotA?.id}`,
        { tag: 'e2e-baseline', notes: 'Created by the FAR-1242 real-stack e2e journey' },
      )
      expect(tagRes.status).toBe(200)
      expect(tagRes.body?.tag).toBe('e2e-baseline')
      const listAfterTag = await apiFetch<{ items: SnapshotResponse[] }>(
        apiBase,
        token,
        'GET',
        `/api/v1/pipelines/${pipeline.id}/snapshots?page_size=50`,
      )
      const tagged = listAfterTag.body?.items.find((s) => s.id === snapshotA?.id)
      expect(tagged?.tag).toBe('e2e-baseline')

      // Structural diff: the second edit ADDED the second node and removed
      // nothing.
      const diffRes = await apiFetch<{ nodes_added: Array<{ id: string }>; nodes_removed: Array<{ id: string }> }>(
        apiBase,
        token,
        'POST',
        `/api/v1/pipelines/${pipeline.id}/snapshots/diff`,
        { snapshot_a_id: snapshotA?.id, snapshot_b_id: snapshotB?.id },
      )
      expect(diffRes.status).toBe(200)
      expect(diffRes.body?.nodes_added.some((n) => n.id === secondNodeId)).toBe(true)
      expect(diffRes.body?.nodes_removed.some((n) => n.id === created.nodeId)).toBe(false)

      // The editor still renders the edited pipeline (the graph write seam).
      await page.goto(`/pipelines/${pipeline.id}/editor`)
      await expect(page.getByTestId('pipeline-editor-toolbar')).toBeVisible({ timeout: 30_000 })
    } finally {
      await cleanupJourneyEntities(cleanup)
    }
  })
})
