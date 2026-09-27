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
 * The binding rides on an `agent` node: the backend refuses a connector
 * binding on a `manual` node ("Manual nodes cannot have connector
 * bindings"), so the journey mints the model backend + agent the node
 * references through the real API before saving the graph.
 *
 * Cleanup: agent, then the model backend it pinned, then the pipeline (with
 * its bound node) and its schema, then the connector.
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
    let backendId: string | null = null
    let agentId: string | null = null
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

      // A connector binding attaches to an `agent` node — the backend refuses
      // it on a `manual` node. Mint the model backend + agent the node
      // references through the real API so the graph can carry the binding.
      const backendRes = await apiFetch<{ id: string }>(apiBase, token, 'POST', '/api/v1/model-backends', {
        name: uniqueName('e2e-journey-connector-backend'),
        display_name: 'E2E Connector Journey Backend',
        provider: 'ollama',
        model_id: 'e2e-journey-model',
        api_key: 'sk-e2e-journey-not-a-real-key',
      })
      if (backendRes.status !== 201 || !backendRes.body?.id) {
        throw new Error(`[realstack] model backend create failed: ${backendRes.status} ${backendRes.text.slice(0, 300)}`)
      }
      backendId = backendRes.body.id

      const agentRes = await apiFetch<{ id: string }>(apiBase, token, 'POST', '/api/v1/agents', {
        name: uniqueName('E2E Connector Agent'),
        description: 'Created by the FAR-1242 connector-binding journey (never executed)',
        input_schema_id: created.schemaId,
        output_schema_id: created.schemaId,
        prompt_template: 'e2e connector-binding journey — never executed',
        model_backend_id: backendId,
        required_environment_capabilities: [],
        template_id: null,
      })
      if (agentRes.status !== 201 || !agentRes.body?.id) {
        throw new Error(`[realstack] agent create failed: ${agentRes.status} ${agentRes.text.slice(0, 300)}`)
      }
      agentId = agentRes.body.id

      // Bind the connector into the agent node. The save-time validator
      // resolves the bound instance — a dangling or inactive binding would be
      // reported in the response's validation issues.
      await setPipelineGraph(apiBase, token, created.pipeline.id, [
        {
          id: created.nodeId,
          node_type: 'agent',
          label: 'E2E Connector Agent',
          position: { x: 120, y: 120 },
          agent_id: agentId,
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
      // The agent pins the schema and backend, so delete it before
      // cleanupJourneyEntities removes the schema. The pipeline graph
      // references the agent, but a graph is JSON — deleting the pipeline
      // after does not re-validate its references.
      if (agentId) {
        try {
          await apiFetch(apiBase, token, 'DELETE', `/api/v1/agents/${agentId}`)
        } catch (err) {
          console.warn('[realstack] cleanup: agent delete failed:', err instanceof Error ? err.message : String(err))
        }
      }
      if (backendId) {
        try {
          await apiFetch(apiBase, token, 'DELETE', `/api/v1/model-backends/${backendId}`)
        } catch (err) {
          console.warn('[realstack] cleanup: model backend delete failed:', err instanceof Error ? err.message : String(err))
        }
      }
      await cleanupJourneyEntities(cleanup)
      if (connectorId) await deleteConnectorBestEffort(apiBase, token, connectorId)
    }
  })
})
