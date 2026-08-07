import { getBaseUrl, getTestEnv, type TestEnv } from './env'

export interface SeedContext {
  pipelineId?: string
  pipelineName: string
}

interface LoginResponse {
  access_token: string
  token_type: string
}

interface ApiPipeline {
  id: string
  name: string
  description: string
  visibility: string
  organisation_id: string
  status: string
  created_at: string
  updated_at: string
}

interface PaginatedResponse<T> {
  items: T[]
  total: number
}

function getApiBaseUrl(target: string): string {
  const envUrl = process.env.E2E_API_URL
  if (envUrl) return envUrl.replace(/\/+$/, '')
  const baseUrl = getBaseUrl(target)
  return baseUrl.replace(/\/+$/, '')
}

async function getAuthToken(apiBase: string): Promise<string> {
  const env = getTestEnv()
  const res = await fetch(apiBase + '/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email: env.credentials.admin.email,
      password: env.credentials.admin.password,
    }),
    signal: AbortSignal.timeout(15000),
  })
  if (!res.ok) {
    throw new Error('Auth failed: ' + res.status + ' ' + res.statusText)
  }
  const data: LoginResponse = await res.json()
  return data.access_token
}

function authHeaders(token: string): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    Authorization: 'Bearer ' + token,
  }
}

async function findExistingPipeline(
  apiBase: string,
  token: string,
): Promise<ApiPipeline | undefined> {
  const res = await fetch(apiBase + '/api/v1/pipelines?page_size=100', {
    headers: authHeaders(token),
    signal: AbortSignal.timeout(15000),
  })
  if (!res.ok) return undefined
  const data: PaginatedResponse<ApiPipeline> = await res.json()
  return data.items.find((p) => p.name.includes('E2E Test'))
}

async function createPipeline(
  apiBase: string,
  token: string,
): Promise<ApiPipeline | undefined> {
  const res = await fetch(apiBase + '/api/v1/pipelines', {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({
      name: 'E2E Test Pipeline',
      description: 'Automatically created by E2E test seeder',
      visibility: 'org',
    }),
    signal: AbortSignal.timeout(15000),
  })
  if (res.status === 501) {
    console.log('[seeder] POST /api/v1/pipelines returned 501, trying starter-pipeline fallback...')
    const fallbackRes = await fetch(apiBase + '/api/v1/onboarding/starter-pipeline', {
      method: 'POST',
      headers: authHeaders(token),
      signal: AbortSignal.timeout(15000),
    })
    if (fallbackRes.ok) {
      return (await fallbackRes.json()) as ApiPipeline
    }
    throw new Error('Pipeline creation failed (primary=501, fallback=' + fallbackRes.status + ')')
  }
  if (!res.ok) {
    throw new Error('Pipeline creation failed: ' + res.status)
  }
  return (await res.json()) as ApiPipeline
}

async function seedPipeline(
  apiBase: string,
  token: string,
): Promise<ApiPipeline | undefined> {
  try {
    const existing = await findExistingPipeline(apiBase, token)
    if (existing) {
      console.log('[seeder] Found existing pipeline: ' + existing.id)
      return existing
    }
    const pipeline = await createPipeline(apiBase, token)
    console.log('[seeder] Created pipeline: ' + pipeline.id)
    return pipeline
  } catch (err) {
    console.error('[seeder] Pipeline seed failed:', err instanceof Error ? err.message : String(err))
    return undefined
  }
}

async function seedRetention(
  apiBase: string,
  token: string,
): Promise<void> {
  try {
    const res = await fetch(apiBase + '/api/v1/admin/runs/retention', {
      method: 'PUT',
      headers: authHeaders(token),
      body: JSON.stringify({ retention_days: 90 }),
      signal: AbortSignal.timeout(15000),
    })
    if (!res.ok) {
      throw new Error('Retention config failed: ' + res.status)
    }
    console.log('[seeder] Set retention to 90 days')
  } catch (err) {
    console.error('[seeder] Retention seed failed:', err instanceof Error ? err.message : String(err))
  }
}

async function seedEmailSettings(
  apiBase: string,
  token: string,
  orgId: string,
): Promise<void> {
  try {
    const res = await fetch(apiBase + '/api/v1/admin/org/' + orgId + '/email-settings', {
      method: 'PUT',
      headers: authHeaders(token),
      body: JSON.stringify({
        smtp_host: 'mail.example.com',
        smtp_port: 587,
        smtp_username: '',
        email_from: 'e2e-test@example.com',
        clear_password: false,
      }),
      signal: AbortSignal.timeout(15000),
    })
    if (!res.ok) {
      throw new Error('Email settings failed: ' + res.status)
    }
    console.log('[seeder] Set email settings')
  } catch (err) {
    console.error('[seeder] Email settings seed failed:', err instanceof Error ? err.message : String(err))
  }
}

async function seedCostControls(
  apiBase: string,
  token: string,
): Promise<void> {
  try {
    const res = await fetch(apiBase + '/api/v1/admin/costs/controls', {
      method: 'PUT',
      headers: authHeaders(token),
      body: JSON.stringify({
        budget: 1000,
        alert_thresholds: [80],
        circuit_breaker_enabled: true,
      }),
      signal: AbortSignal.timeout(15000),
    })
    if (!res.ok) {
      throw new Error('Cost controls failed: ' + res.status)
    }
    console.log('[seeder] Set cost controls')
  } catch (err) {
    console.error('[seeder] Cost controls seed failed:', err instanceof Error ? err.message : String(err))
  }
}

/**
 * Promote the org to the "team" tier via the org-profile update endpoint.
 * The backend has no "enterprise" tier — team is the highest. Team-gated admin
 * endpoints (run retention, cost controls, environment profiles, remy admin)
 * return 402 on a community-tier org, so the seeder must establish the tier.
 */
async function establishTeamTier(apiBase: string, token: string): Promise<boolean> {
  try {
    const res = await fetch(apiBase + '/api/v1/admin/org', {
      method: 'PUT',
      headers: authHeaders(token),
      body: JSON.stringify({ plan_id: 'team' }),
      signal: AbortSignal.timeout(15000),
    })
    if (!res.ok) {
      console.error('[seeder] Could not set org plan to team: ' + res.status + ' ' + res.statusText)
      return false
    }
    const flagsRes = await fetch(apiBase + '/api/v1/admin/feature-flags', {
      headers: authHeaders(token),
      signal: AbortSignal.timeout(15000),
    })
    if (flagsRes.ok) {
      const data = (await flagsRes.json()) as { license?: { tier?: string } }
      const tier = data?.license?.tier ?? 'unknown'
      console.log('[seeder] Org tier after update: "' + tier + '"')
      return tier === 'team'
    }
    return true
  } catch (err) {
    console.error('[seeder] Failed to establish team tier:', err instanceof Error ? err.message : String(err))
    return false
  }
}

export async function seedTargetEnvironment(env: TestEnv): Promise<SeedContext> {
  const ctx: SeedContext = { pipelineName: 'E2E Test Pipeline' }

  if (env.name === 'local') {
    console.log('[seeder] Local target detected, skipping data seeding')
    return ctx
  }

  const apiBase = getApiBaseUrl(env.name)
  console.log('[seeder] Seeding target "' + env.name + '" at ' + apiBase + '...')

  let token: string
  try {
    token = await getAuthToken(apiBase)
  } catch (err) {
    console.error('[seeder] Failed to authenticate, cannot seed:', err instanceof Error ? err.message : String(err))
    return ctx
  }

  const teamTier = await establishTeamTier(apiBase, token)
  if (!teamTier) {
    console.error(
      '[seeder] WARNING: org is NOT team-tier. Team-gated E2E features (run retention, cost controls, ' +
        'environment profiles, remy admin) will be unavailable. Upgrade the org plan_id to "team" ' +
        '(PUT /api/v1/admin/org) or set MODULO_LICENSE_KEY on the deployment.',
    )
  }

  const pipeline = await seedPipeline(apiBase, token)
  ctx.pipelineId = pipeline?.id

  await seedRetention(apiBase, token)

  if (pipeline?.organisation_id) {
    await seedEmailSettings(apiBase, token, pipeline.organisation_id)
  }

  await seedCostControls(apiBase, token)

  console.log('[seeder] Seeding complete')
  return ctx
}
