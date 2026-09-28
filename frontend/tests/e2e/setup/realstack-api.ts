import type { TestEnv } from './env'
import { getBaseUrl } from './env'

/**
 * Real-stack journey harness (FAR-1242; batch 1 core + batch 2 additions).
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
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE',
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
  /** Agent reference for `agent` / `sandbox_agent` nodes (batch 2). */
  agent_id?: string | null
  /** Node-level connector binding (batch 2): { type, instance_id }. */
  connector_binding?: { type: string; instance_id: string } | null
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
  /** Cancellation transparency: closed-vocabulary reason + acting account. */
  cancel_reason?: string | null
  cancelled_by?: string | null
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

/** Best-effort pipeline deletion (404-tolerant). Never throws over the caller. */
export async function deletePipelineBestEffort(apiBase: string, token: string, pipelineId: string): Promise<void> {
  try {
    await deletePipeline(apiBase, token, pipelineId)
  } catch (err) {
    console.warn('[realstack] cleanup: pipeline delete failed:', err instanceof Error ? err.message : String(err))
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

export interface SchemaListResponse {
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

// ---------------------------------------------------------------------------
// Batch 2 additions (FAR-1242): triggers, HITL gates, run IO, users, connectors
// ---------------------------------------------------------------------------

interface TriggerResponse {
  id: string
  name: string | null
  trigger_type: string
  active: boolean
  next_fire_at: string | null
  /** Void after a real delivery: when the trigger last created a run. */
  last_fired_at?: string | null
}

export type TriggerRef = TriggerResponse

export interface TriggerCreateBody {
  trigger_type: 'cron' | 'manual' | 'webhook' | 'polling' | 'agent_signal' | 'ongoing' | 'slack_app_mention'
  name?: string
  active?: boolean
  cron_expression?: string
  cron_timezone?: string
  config_json?: Record<string, unknown>
}

/** Create a trigger on a pipeline (POST /api/v1/pipelines/{id}/triggers). */
export async function createTrigger(
  apiBase: string,
  token: string,
  pipelineId: string,
  body: TriggerCreateBody,
): Promise<TriggerRef> {
  const res = await apiFetch<TriggerResponse>(apiBase, token, 'POST', `/api/v1/pipelines/${pipelineId}/triggers`, body)
  if (res.status !== 201 || !res.body?.id) {
    throw new Error(`[realstack] trigger create failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
  return res.body
}

interface TriggerListResponse {
  items: TriggerResponse[]
}

/** List triggers, optionally scoped to one pipeline. */
export async function listTriggers(apiBase: string, token: string, pipelineId?: string): Promise<TriggerListResponse> {
  const query = pipelineId ? `?pipeline_id=${pipelineId}&page_size=100` : '?page_size=100'
  const res = await apiFetch<TriggerListResponse>(apiBase, token, 'GET', `/api/v1/triggers${query}`)
  if (res.status !== 200) {
    throw new Error(`[realstack] trigger list failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
  return res.body ?? { items: [] }
}

/** Delete a trigger (soft-delete). 404-tolerant so cleanup is idempotent. */
export async function deleteTrigger(apiBase: string, token: string, triggerId: string): Promise<void> {
  const res = await apiFetch(apiBase, token, 'DELETE', `/api/v1/triggers/${triggerId}`)
  if (res.status === 404) return
  if (res.status !== 204) {
    throw new Error(`[realstack] trigger delete failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
}

export interface PendingReview {
  review_id: string
  decision: string | null
  label: string | null
}

interface PendingReviewsResponse {
  reviews: PendingReview[]
}

/** List a run's pending (undecided) HITL reviews. */
export async function getRunPendingReviews(apiBase: string, token: string, runId: string): Promise<PendingReview[]> {
  const res = await apiFetch<PendingReviewsResponse>(apiBase, token, 'GET', `/api/v1/runs/${runId}/hitl/pending`)
  if (res.status !== 200) {
    throw new Error(`[realstack] pending-review list failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
  return res.body?.reviews ?? []
}

/** Claim a HITL gate through the real API, returning the claim token. */
export async function claimGate(apiBase: string, token: string, runId: string, gateId: string): Promise<string> {
  const res = await apiFetch<{ claim_token: string }>(
    apiBase,
    token,
    'POST',
    `/api/v1/runs/${runId}/hitl/${gateId}/claim`,
    {},
  )
  if (res.status !== 200 || !res.body?.claim_token) {
    throw new Error(`[realstack] gate claim failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
  return res.body.claim_token
}

export interface RunIoResponse {
  run_id: string
  status: string
  input_payload: Record<string, unknown> | null
  outputs_json: Record<string, unknown> | null
}

/** Fetch a run's per-node IO (normalized, masked view). */
export async function getRunIo(apiBase: string, token: string, runId: string): Promise<ApiResult<RunIoResponse>> {
  return apiFetch<RunIoResponse>(apiBase, token, 'GET', `/api/v1/runs/${runId}/io`)
}

export interface ManualNodePipeline {
  pipeline: PipelineRef
  schemaId: string
  nodeId: string
}

/**
 * Create a pipeline whose graph is a single manual-input node bound to a
 * fresh output schema (the backend requires manual nodes to declare an
 * output schema and a label). A manual node never calls an LLM: it interrupts
 * the run until a human supplies a decision. Shared by batch 2's run journeys;
 * additive so later batches can reuse it.
 */
export async function createManualNodePipeline(
  apiBase: string,
  token: string,
  pipelineName: string,
  schemaName: string,
): Promise<ManualNodePipeline> {
  const schemaId = await createSchemaWithVersion(apiBase, token, schemaName)
  let pipeline: PipelineRef | null = null
  try {
    pipeline = await createPipeline(apiBase, token, pipelineName)
    const nodeId = crypto.randomUUID()
    await setPipelineGraph(apiBase, token, pipeline.id, [
      {
        id: nodeId,
        node_type: 'manual',
        label: 'E2E Human Input',
        position: { x: 120, y: 120 },
        output_schema_id: schemaId,
      },
    ])
    return { pipeline, schemaId, nodeId }
  } catch (err) {
    // A partial creation must not leak: the caller only ever receives ids on
    // success, so delete whatever was created before rethrowing.
    await cleanupJourneyEntities({
      pipelineIds: pipeline ? [pipeline.id] : [],
      schemaIds: [schemaId],
      token,
      apiBase,
    })
    throw err
  }
}

export interface JourneyUser {
  id: string
  email: string
}

/**
 * Create a real user in the caller's org through POST /api/v1/admin/users.
 * The account is minted with must_change_password=true (the forced-rotation
 * gate), so its first sign-in must rotate the password.
 */
export async function createJourneyUser(
  apiBase: string,
  adminToken: string,
  req: { email: string; display_name: string; password: string; org_role: string },
): Promise<JourneyUser> {
  const res = await apiFetch<JourneyUser>(apiBase, adminToken, 'POST', '/api/v1/admin/users', req)
  if (res.status !== 201 || !res.body?.id) {
    throw new Error(`[realstack] user create failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
  return res.body
}

/**
 * Best-effort cleanup: deactivate a journey-created user (tombstones the org
 * membership). Never throws over the caller's own result.
 */
export async function deactivateJourneyUser(apiBase: string, adminToken: string, userId: string): Promise<void> {
  try {
    const res = await apiFetch(apiBase, adminToken, 'PUT', `/api/v1/admin/users/${userId}`, { is_active: false })
    if (res.status !== 200) {
      console.warn(`[realstack] cleanup: user ${userId} deactivate returned ${res.status}`)
    }
  } catch (err) {
    console.warn('[realstack] cleanup: user deactivate failed:', err instanceof Error ? err.message : String(err))
  }
}

/** Best-effort connector deletion (404-tolerant). Never throws over the caller. */
export async function deleteConnectorBestEffort(apiBase: string, token: string, connectorId: string): Promise<void> {
  try {
    const res = await apiFetch(apiBase, token, 'DELETE', `/api/v1/connectors/${connectorId}`)
    if (res.status !== 204 && res.status !== 404) {
      console.warn(`[realstack] cleanup: connector ${connectorId} delete returned ${res.status}`)
    }
  } catch (err) {
    console.warn('[realstack] cleanup: connector delete failed:', err instanceof Error ? err.message : String(err))
  }
}

// ---------------------------------------------------------------------------
// Batch 3 additions (FAR-1242): teams, lifecycle maps, webhooks, evals,
// parameter schemas — plus a real API cancel for webhook-run cleanup.
// ---------------------------------------------------------------------------

export interface AdminTeam {
  id: string
  name: string
  description: string | null
  member_count?: number
  owned_resource_count?: number | null
}

interface AdminTeamListResponse {
  items: AdminTeam[]
}

/** List the org's teams (admin). */
export async function listTeams(apiBase: string, token: string): Promise<AdminTeamListResponse> {
  const res = await apiFetch<AdminTeamListResponse>(apiBase, token, 'GET', '/api/v1/admin/teams')
  if (res.status !== 200) {
    throw new Error(`[realstack] team list failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
  return res.body ?? { items: [] }
}

/** Best-effort team deletion (404-tolerant). Never throws over the caller. */
export async function deleteTeamBestEffort(apiBase: string, token: string, teamId: string): Promise<void> {
  try {
    const res = await apiFetch(apiBase, token, 'DELETE', `/api/v1/admin/teams/${teamId}`)
    if (res.status !== 204 && res.status !== 404) {
      console.warn(`[realstack] cleanup: team ${teamId} delete returned ${res.status}`)
    }
  } catch (err) {
    console.warn('[realstack] cleanup: team delete failed:', err instanceof Error ? err.message : String(err))
  }
}

export interface LifecycleMapDetail {
  id: string
  name: string
  current_version: number
  stages: Array<Record<string, unknown>>
  archived_at: string | null
}

/** Fetch a lifecycle map's persisted detail. */
export async function getLifecycleMap(apiBase: string, token: string, mapId: string): Promise<ApiResult<LifecycleMapDetail>> {
  return apiFetch<LifecycleMapDetail>(apiBase, token, 'GET', `/api/v1/lifecycle-maps/${mapId}`)
}

/** Best-effort lifecycle map deletion (404-tolerant). Never throws over the caller. */
export async function deleteLifecycleMapBestEffort(apiBase: string, token: string, mapId: string): Promise<void> {
  try {
    const res = await apiFetch(apiBase, token, 'DELETE', `/api/v1/lifecycle-maps/${mapId}`)
    if (res.status !== 204 && res.status !== 404) {
      console.warn(`[realstack] cleanup: lifecycle map ${mapId} delete returned ${res.status}`)
    }
  } catch (err) {
    console.warn('[realstack] cleanup: lifecycle map delete failed:', err instanceof Error ? err.message : String(err))
  }
}

/**
 * Fire a webhook trigger with a raw HTTP delivery from OUTSIDE the app —
 * the exact boundary an external sender crosses. Deliberately no auth
 * headers: HMAC-less triggers accept unauthenticated deliveries by design.
 */
export async function fireWebhook(apiBase: string, triggerId: string, payload: Record<string, unknown>): Promise<ApiResult<{ run_id: string | null; status: string }>> {
  const res = await fetch(`${apiBase}/api/v1/triggers/${triggerId}/webhook`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(20_000),
  })
  const text = await res.text()
  let parsed: { run_id: string | null; status: string } | null = null
  try {
    parsed = JSON.parse(text) as { run_id: string | null; status: string }
  } catch {
    parsed = null
  }
  return { status: res.status, body: parsed, text }
}

/** Request cancellation through the real API (202 — terminalised async). */
export async function cancelRun(apiBase: string, token: string, runId: string): Promise<void> {
  const res = await apiFetch(apiBase, token, 'POST', `/api/v1/runs/${runId}/cancel`, {})
  if (res.status !== 202) {
    throw new Error(`[realstack] run cancel failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
}

export interface EvalDefinitionItem {
  id: string
  pipeline_id: string
  name: string
  eval_type: string
}

interface EvalListResponse {
  items: EvalDefinitionItem[]
}

/** List eval definitions scoped to one pipeline. */
export async function listEvals(apiBase: string, token: string, pipelineId: string): Promise<EvalListResponse> {
  const res = await apiFetch<EvalListResponse>(apiBase, token, 'GET', `/api/v1/evals?pipeline_id=${pipelineId}`)
  if (res.status !== 200) {
    throw new Error(`[realstack] eval list failed: ${res.status} ${res.text.slice(0, 300)}`)
  }
  return res.body ?? { items: [] }
}

/** Best-effort eval definition deletion (404-tolerant). Never throws. */
export async function deleteEvalBestEffort(apiBase: string, token: string, evalId: string): Promise<void> {
  try {
    const res = await apiFetch(apiBase, token, 'DELETE', `/api/v1/evals/${evalId}`)
    if (res.status !== 200 && res.status !== 204 && res.status !== 404) {
      console.warn(`[realstack] cleanup: eval ${evalId} delete returned ${res.status}`)
    }
  } catch (err) {
    console.warn('[realstack] cleanup: eval delete failed:', err instanceof Error ? err.message : String(err))
  }
}

/**
 * Admin precondition: the target exposes the parameter-schema surface
 * (team-tier feature). Journeys that need it must skip cleanly when the
 * licence does not include it, instead of failing.
 */
export async function hasParameterSchemaAccess(apiBase: string, token: string): Promise<boolean> {
  const res = await apiFetch(apiBase, token, 'GET', '/api/v1/parameter-schemas?page=1&page_size=1')
  return res.status === 200
}

/** Best-effort parameter schema deletion (page 1 lists + clean 200/404). */
export async function deleteParameterSchemaBestEffort(apiBase: string, token: string, schemaId: string): Promise<void> {
  try {
    const res = await apiFetch(apiBase, token, 'DELETE', `/api/v1/parameter-schemas/${schemaId}`)
    if (res.status !== 200 && res.status !== 204 && res.status !== 404) {
      console.warn(`[realstack] cleanup: parameter schema ${schemaId} delete returned ${res.status}`)
    }
  } catch (err) {
    console.warn('[realstack] cleanup: parameter schema delete failed:', err instanceof Error ? err.message : String(err))
  }
}
