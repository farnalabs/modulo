import type { TestEnv } from './env'
import { getBaseUrl } from './env'

/**
 * Real-stack journey harness (FAR-1242, batch 1).
 *
 * Journeys that import this helper drive the REAL backend of the configured
 * target (staging / app). They must never `page.route`-mock the endpoint under
 * test — no HTTP mocking is done here at all. These helpers perform
 * out-of-band fetches from the test process (the same pattern as
 * setup/seeder.ts) purely for ARRANGE and CLEANUP; the observable assertions
 * look at the rendered UI and/or the persisted state the backend reports.
 *
 * Every journey using this harness must:
 *  - skip the local target (`env.name === 'local'`), where the whole API is
 *    page.route-mocked by setupLocalMockApi and no real backend exists;
 *  - create the data it needs with unique names (shared instance) and delete
 *    everything it created in a finally block.
 */

export interface ApiResult<T = unknown> {
  status: number
  body: T | null
  text: string
}

export function apiBaseFor(env: TestEnv): string {
  const explicit = process.env.E2E_API_URL
  return (explicit || getBaseUrl(env.name)).replace(/\/+$/, '')
}

interface LoginResponse {
  access_token: string
}

export async function apiLogin(env: TestEnv): Promise<string> {
  const res = await fetch(apiBaseFor(env) + '/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email: env.credentials.admin.email,
      password: env.credentials.admin.password,
    }),
    signal: AbortSignal.timeout(20_000),
  })
  if (!res.ok) {
    throw new Error(`[realstack] login failed: ${res.status} ${res.statusText}`)
  }
  const data = (await res.json()) as LoginResponse
  return data.access_token
}

export async function apiFetch<T>(
  apiBase: string,
  token: string,
  method: 'GET' | 'POST' | 'PATCH' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<ApiResult<T>> {
  const res = await fetch(apiBase + path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(20_000),
  })
  const text = await res.text()
  let parsed: T | null = null
  if (text) {
    try {
      parsed = JSON.parse(text) as T
    } catch {
      parsed = null
    }
  }
  return { status: res.status, body: parsed, text }
}

/** Unique, human-readable name so parallel seeding never collides. */
export function uniqueName(prefix: string): string {
  return `${prefix} ${Date.now().toString(36)}-${Math.floor(Math.random() * 1e8).toString(36)}`
}

export interface PipelineRef {
  id: string
  name: string
}

interface PipelineResponse {
  id: string
  name: string
}

export async function createPipeline(apiBase: string, token: string, name: string): Promise<PipelineRef> {
  const res = await apiFetch<PipelineResponse>(apiBase, token, 'POST', '/api/v1/pipelines', {
    name,
    description: 'Created by the FAR-1242 real-stack e2e journeys',
    visibility: 'org',
  })
  if (res.status !== 201 || !res.body?.id) {
    throw new Error(`[realstack] pipeline create failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
  return { id: res.body.id, name: res.body.name }
}

export interface GraphNode {
  id: string
  node_type: string
  label?: string | null
  position: { x: number; y: number }
  output_schema_id?: string | null
}

export async function setPipelineGraph(
  apiBase: string,
  token: string,
  pipelineId: string,
  nodes: GraphNode[],
  edges: Array<Record<string, unknown>> = [],
): Promise<void> {
  const res = await apiFetch(apiBase, token, 'PATCH', `/api/v1/pipelines/${pipelineId}/graph`, {
    nodes,
    edges,
  })
  if (res.status !== 200) {
    throw new Error(`[realstack] graph save failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
}

export interface RunResponse {
  run_id: string
  status: string
  pipeline_id: string
  pipeline_name?: string | null
  error_detail?: string | null
}

export async function triggerRun(
  apiBase: string,
  token: string,
  pipelineId: string,
  inputPayload: Record<string, unknown> = {},
): Promise<RunResponse> {
  const res = await apiFetch<RunResponse>(apiBase, token, 'POST', '/api/v1/runs', {
    pipeline_id: pipelineId,
    input_payload: inputPayload,
  })
  if (res.status !== 202 || !res.body?.run_id) {
    throw new Error(`[realstack] run trigger failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
  return res.body
}

export async function getRun(apiBase: string, token: string, runId: string): Promise<ApiResult<RunResponse>> {
  return apiFetch<RunResponse>(apiBase, token, 'GET', `/api/v1/runs/${runId}`)
}

const RUN_POLL_INTERVAL_MS = 2_000

/**
 * Poll the run's status through the real API until `predicate` holds.
 * Throws with the last observed status when `timeoutMs` elapses — a journey
 * must never assert against an unobserved state.
 */
export async function pollRunStatus(
  apiBase: string,
  token: string,
  runId: string,
  predicate: (status: string) => boolean,
  opts: { timeoutMs?: number } = {},
): Promise<string> {
  const timeoutMs = opts.timeoutMs ?? 90_000
  const deadline = Date.now() + timeoutMs
  let last = 'unknown'
  while (Date.now() < deadline) {
    const res = await getRun(apiBase, token, runId)
    if (res.status === 200 && res.body?.status) {
      last = res.body.status
      if (predicate(last)) return last
    }
    await new Promise((resolve) => setTimeout(resolve, RUN_POLL_INTERVAL_MS))
  }
  throw new Error(`[realstack] run ${runId} never reached the expected status (last: ${last})`)
}

export async function deletePipeline(apiBase: string, token: string, pipelineId: string): Promise<void> {
  const res = await apiFetch(apiBase, token, 'DELETE', `/api/v1/pipelines/${pipelineId}`)
  if (res.status === 404) return
  if (res.status !== 204) {
    throw new Error(`[realstack] pipeline delete failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
}

export async function deleteSchema(apiBase: string, token: string, schemaId: string): Promise<void> {
  let res = await apiFetch(apiBase, token, 'DELETE', `/api/v1/schemas/${schemaId}`)
  if (res.status === 409) {
    // Deletion protection (e.g. still referenced) — the caller should have
    // deleted the referencing pipeline first; force as a last resort.
    res = await apiFetch(apiBase, token, 'DELETE', `/api/v1/schemas/${schemaId}?force=true`)
  }
  if (res.status === 404) return
  if (res.status !== 204) {
    throw new Error(`[realstack] schema delete failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
}

interface SchemaListResponse {
  items: Array<{ id: string; name: string }>
}

/**
 * Create a schema with one published 1.0.0 version defining a single string
 * field — the minimal valid output schema a manual node requires.
 * Returns the schema id (for cleanup).
 */
export async function createSchemaWithVersion(apiBase: string, token: string, name: string): Promise<string> {
  const createRes = await apiFetch<{ id: string }>(apiBase, token, 'POST', '/api/v1/schemas', {
    name,
    description: 'Created by the FAR-1242 real-stack e2e journeys',
  })
  if (createRes.status !== 201 || !createRes.body?.id) {
    throw new Error(`[realstack] schema create failed: ${createRes.status} ${createRes.text.slice(0, 300)}`)
  }
  const schemaId = createRes.body.id
  const versionRes = await apiFetch(apiBase, token, 'POST', `/api/v1/schemas/${schemaId}/versions`, {
    version: '1.0.0',
    version_number: 1,
    definition_json: {
      $schema: 'https://json-schema.org/draft/2020-12/schema',
      title: name,
      type: 'object',
      properties: {
        approved_output: { type: 'string', description: 'Human-provided output' },
      },
      required: ['approved_output'],
    },
    published: true,
  })
  if (versionRes.status !== 201) {
    await deleteSchema(apiBase, token, schemaId)
    throw new Error(`[realstack] schema version create failed: ${versionRes.status} ${versionRes.text.slice(0, 300)}`)
  }
  return schemaId
}

export interface JourneyCleanup {
  pipelineIds: string[]
  schemaIds: string[]
  token: string
  apiBase: string
}

/**
 * Best-effort finally-block cleanup: delete what the journey created, in
 * dependency order (pipelines before schemas — a schema referenced by a live
 * graph is deletion-protected). Never throws over the original failure.
 */
export async function cleanupJourneyEntities(cleanup: JourneyCleanup): Promise<void> {
  for (const pipelineId of cleanup.pipelineIds) {
    try {
      await deletePipeline(cleanup.apiBase, cleanup.token, pipelineId)
    } catch (err) {
      console.warn(`[realstack] cleanup: pipeline ${pipelineId} delete failed:`, err instanceof Error ? err.message : String(err))
    }
  }
  for (const schemaId of cleanup.schemaIds) {
    try {
      await deleteSchema(cleanup.apiBase, cleanup.token, schemaId)
    } catch (err) {
      console.warn(`[realstack] cleanup: schema ${schemaId} delete failed:`, err instanceof Error ? err.message : String(err))
    }
  }
}
