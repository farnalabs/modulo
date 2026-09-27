import { test, expect, loginAsAdmin } from '../setup/fixtures'
import {
  apiBaseFor,
  apiFetch,
  apiLogin,
  cleanupJourneyEntities,
  createManualNodePipeline,
  deleteConnectorBestEffort,
  setPipelineGraph,
  uniqueName,
  type JourneyCleanup,
} from '../setup/realstack-api'

/**
 * Real-stack connector-binding journey (FAR-1242 batch 2).
 *
 * Runs against the REAL backend (staging/app); skips the local target. The
 * connector hub is the credential seam every node's capability contract rides
 * on, yet no existing spec exercises a real connector row. This journey
 * creates a connector through the real admin form, binds it into a pipeline
 * graph, and proves the binding persisted on re-read — the persisted state
 * that changes if the binding path is deleted.
 *
 * Cleanup: pipeline (with its bound node) first, then the connector.
 */

interface ConnectorListItem {
  id: string
  name: string
  connector_type_id: string
  has_credentials: boolean
  status: string
}

interface GraphNodeEcho {
  id: string
  node_type: string
  connector_binding: { type: string; instance_id: string } | null
}

test.describe('Real-stack journeys: connector binding', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('creating a connector in the admin form persists it, and binding it into a graph persists the binding', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)
    const connectorName = uniqueName('e2e-journey-connector')
    const cleanup: JourneyCleanup = { pipelineIds: [], schemaIds: [], token, apiBase }
    let connectorId: string | null = null
    const created = await createManualNodePipeline(
      apiBase,
      token,
      uniqueName('E2E Journey Connector Pipeline'),
      uniqueName('E2E Connector Schema'),
    )
    cleanup.pipelineIds.push(created.pipeline.id)
    cleanup.schemaIds.push(created.schemaId)
    try {
      await loginAsAdmin(page, env)
      await page.goto('/admin/connectors')

      // Create through the real admin form: name -> Filesystem type ->
      // description -> the config textarea (the non-REST connector's
      // credential payload on this form) -> submit.
      await page.getByTestId('admin-connectors-add').click()
      await page.getByTestId('admin-connectors-name-input').fill(connectorName)
      await page.getByTestId('admin-connectors-type-select').click()
      await page.locator('[data-value="filesystem"]').click()
      await page.getByTestId('admin-connectors-description-input').fill('Created by the FAR-1242 real-stack e2e journey')
      await page.getByTestId('admin-connectors-config-input').fill('e2e-journey-noop-credentials')
      await page.getByTestId('admin-connectors-submit').click()

      // Observable effect: the row renders in the connector list...
      const row = page.locator('tr[data-testid^="connector-row-"]').filter({ hasText: connectorName })
      await expect(row.first()).toBeVisible({ timeout: 30_000 })

      // ...and the connector really persisted (credentials held server-side).
      const listRes = await apiFetch<{ items: ConnectorListItem[] }>(apiBase, token, 'GET', '/api/v1/connectors?page_size=100')
      expect(listRes.status).toBe(200)
      const createdConnector = listRes.body?.items.find((c) => c.name === connectorName)
      expect(createdConnector, 'created connector must be returned by GET /api/v1/connectors').toBeTruthy()
      expect(createdConnector?.connector_type_id).toBe('filesystem')
      expect(createdConnector?.has_credentials).toBe(true)
      expect(createdConnector?.status).toBe('active')
      connectorId = createdConnector?.id ?? null
      if (!connectorId) throw new Error('[realstack] created connector id could not be resolved')

      // Bind the connector into the pipeline's graph node. The save-time
      // validator resolves the bound instance — a dangling or inactive
      // binding would be reported in the response's validation issues.
      await setPipelineGraph(apiBase, token, created.pipeline.id, [
        {
          id: created.nodeId,
          node_type: 'manual',
          label: 'E2E Human Input',
          position: { x: 120, y: 120 },
          output_schema_id: created.schemaId,
          connector_binding: { type: 'filesystem', instance_id: connectorId },
        },
      ])

      // Persisted: a real re-read echoes the binding on the node.
      const graphRes = await apiFetch<{ nodes: GraphNodeEcho[] }>(apiBase, token, 'GET', `/api/v1/pipelines/${created.pipeline.id}/graph`)
      expect(graphRes.status).toBe(200)
      const boundNode = graphRes.body?.nodes.find((n) => n.id === created.nodeId)
      expect(boundNode?.connector_binding).toEqual({ type: 'filesystem', instance_id: connectorId })

      // Remove the connector and prove it is really gone from the backend.
      const del = await apiFetch(apiBase, token, 'DELETE', `/api/v1/connectors/${connectorId}`)
      expect(del.status).toBe(204)
      const goneRes = await apiFetch<ConnectorListItem>(apiBase, token, 'GET', `/api/v1/connectors/${connectorId}`)
      expect(goneRes.status).toBe(404)
      connectorId = null
    } finally {
      await cleanupJourneyEntities(cleanup)
      if (connectorId) await deleteConnectorBestEffort(apiBase, token, connectorId)
    }
  })
})
