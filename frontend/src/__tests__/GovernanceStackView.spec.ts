import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick as vueNextTick } from 'vue'

async function nextTick() {
  await vueNextTick()
  await flushPromises()
}

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    POST: vi.fn(),
    PUT: vi.fn(),
    PATCH: vi.fn(),
    DELETE: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import GovernanceStackView from '../views/GovernanceStackView.vue'

const LEVEL_IDS = ['models', 'tools', 'mcp', 'agents', 'commands', 'autonomy', 'telemetry'] as const

function ok<T>(data: T) {
  return { data, error: undefined, response: { status: 200 } }
}

type Override = unknown | (() => unknown)

/**
 * Route every endpoint the view composes to a default success payload, with
 * per-URL overrides. An override that is a function is invoked per call, so a
 * test can flip a level from failing to healthy for the retry assertion.
 */
function mockDefaults(overrides: Record<string, Override> = {}) {
  mockGet.mockImplementation(async (url: string) => {
    const override = overrides[url]
    if (override !== undefined) {
      return typeof override === 'function' ? (override as () => unknown)() : override
    }
    switch (url) {
      case '/api/v1/model-backends':
        return ok({ items: [{ id: 'm1' }, { id: 'm2' }], total: 2, page: 1, page_size: 1 })
      case '/api/v1/connectors':
        return ok({ items: [], total: 0, page: 1, page_size: 1, has_more: false })
      case '/api/v1/mcp/oauth/clients':
        return ok([{ id: 'client-1' }])
      case '/api/v1/api-keys':
        return ok([{ id: 'key-1' }])
      case '/api/v1/agents':
        return ok({ items: [{ id: 'agent-1' }], total: 3, page: 1, page_size: 1 })
      case '/api/v1/composite-templates':
        return ok({ items: [], total: 4, page: 1, page_size: 1 })
      case '/api/v1/libraries':
        return ok({ items: [], total: 5, page: 1, page_size: 1, has_more: false })
      case '/api/v1/pipelines':
        return ok({
          items: [
            { earned_autonomy_level: 'fully_autonomous', default_autonomy_level: 'manual_approval' },
            { earned_autonomy_level: null, default_autonomy_level: 'notify_on_complete' },
            { default_autonomy_level: 'manual_approval' },
          ],
          total: 4,
          page: 1,
          page_size: 100,
          has_more: false,
        })
      case '/api/v1/settings/observability':
        return ok({
          otlp_endpoint: 'http://otel:4318',
          otlp_headers: {},
          export_interval_seconds: 30,
          langsmith_enabled: false,
          has_langsmith_api_key: false,
          effective_otlp_endpoint: 'http://otel:4318',
          env_override_active: false,
        })
      case '/api/v1/errors':
        return ok({ items: [], total: 2, limit: 1, offset: 0 })
      case '/api/v1/notifications':
        return ok([{ id: 'endpoint-1' }, { id: 'endpoint-2' }])
      default:
        return { data: undefined, error: { detail: `unrouted: ${url}` }, response: { status: 500 } }
    }
  })
}

function mountView() {
  return mount(GovernanceStackView, {
    global: {
      stubs: {
        FeatureGate: { template: '<div><slot /></div>' },
      },
    },
  })
}

function links(wrapper: ReturnType<typeof mountView>, levelId: string) {
  return wrapper.find(`[data-testid="governance-level-${levelId}"]`).findAll('a')
}

beforeEach(() => {
  vi.clearAllMocks()
  mockDefaults()
})

describe('GovernanceStackView — hierarchy readout', () => {
  it('renders all seven levels with a live count and configured/empty state', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    for (const id of LEVEL_IDS) {
      expect(wrapper.find(`[data-testid="governance-level-${id}"]`).exists()).toBe(true)
      expect(wrapper.find(`[data-testid="governance-level-ready-${id}"]`).exists()).toBe(true)
    }

    const count = (id: string) => wrapper.find(`[data-testid="governance-level-count-${id}"]`).text()
    expect(count('models')).toContain('2 model backends')
    expect(count('tools')).toContain('0 connectors')
    expect(count('mcp')).toContain('2 MCP clients or keys')
    expect(count('agents')).toContain('3 agents')
    expect(count('commands')).toContain('9 reusable units')
    expect(count('autonomy')).toContain('4 pipelines')
    expect(count('telemetry')).toContain('4 telemetry destinations')

    expect(wrapper.find('[data-testid="governance-level-state-models"]').text()).toBe('Configured')
    expect(wrapper.find('[data-testid="governance-level-state-tools"]').text()).toBe('Empty')
  })

  it('renders the empty state when a level has no configured entities', async () => {
    mockDefaults({
      '/api/v1/model-backends': ok({ items: [], total: 0, page: 1, page_size: 1 }),
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-state-models"]').text()).toBe('Empty')
    expect(wrapper.find('[data-testid="governance-level-count-models"]').text()).toContain('0 model backends')
  })

  it('summarises the autonomy ladder across the three rungs', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const detail = wrapper.find('[data-testid="governance-level-detail-autonomy"]').text()
    expect(detail).toContain('Manual approval: 1')
    expect(detail).toContain('Notify on complete: 1')
    expect(detail).toContain('Fully autonomous: 1')
  })

  it('deep-links each level to its management surface', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(links(wrapper, 'models')[0].attributes('href')).toBe('/admin/model-backends')
    expect(links(wrapper, 'tools')[0].attributes('href')).toBe('/admin/connectors')
    expect(links(wrapper, 'mcp')[0].attributes('href')).toBe('/settings/mcp')
    expect(links(wrapper, 'agents')[0].attributes('href')).toBe('/library')
    expect(links(wrapper, 'commands')[0].attributes('href')).toBe('/library')
    expect(links(wrapper, 'autonomy')[0].attributes('href')).toBe('/pipelines')
    expect(links(wrapper, 'telemetry').map((l) => l.attributes('href'))).toEqual([
      '/settings/observability',
      '/admin/errors',
      '/admin/notification-delivery',
    ])
  })

  it('states the not-yet-governed boundaries without fabricating data', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-note-mcp"]').text()).toContain('not yet governed')
    expect(wrapper.find('[data-testid="governance-level-note-agents"]').text()).toContain('not yet governed')
    expect(wrapper.find('[data-testid="governance-level-note-commands"]').text()).toContain(
      'composite templates and library primitives',
    )
  })
})

describe('GovernanceStackView — per-level isolation', () => {
  it('marks only the restricted level when one endpoint returns 403', async () => {
    mockDefaults({
      '/api/v1/connectors': {
        data: undefined,
        error: { status: 403, detail: 'forbidden' },
        response: { status: 403 },
      },
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-restricted-tools"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="governance-level-ready-tools"]').exists()).toBe(false)
    // The remaining levels still render their data — the page is not blanked.
    expect(wrapper.find('[data-testid="governance-level-ready-models"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="governance-level-ready-autonomy"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="governance-level-ready-telemetry"]').exists()).toBe(true)
  })

  it('marks a 402 feature-gated level restricted as well', async () => {
    mockDefaults({
      '/api/v1/libraries': {
        data: undefined,
        error: { status: 402, detail: 'feature required' },
        response: { status: 402 },
      },
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-restricted-commands"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="governance-level-ready-commands"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="governance-level-ready-tools"]').exists()).toBe(true)
  })

  it('shows a retry-able per-level error on transport failure and recovers on retry', async () => {
    let failing = true
    mockDefaults({
      '/api/v1/agents': () => {
        if (failing) throw new Error('agents down')
        return ok({ items: [], total: 0, page: 1, page_size: 1 })
      },
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const error = wrapper.find('[data-testid="governance-level-error-agents"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('agents down')
    expect(error.attributes('aria-live')).toBe('assertive')
    // Other levels are unaffected by the failure.
    expect(wrapper.find('[data-testid="governance-level-ready-models"]').exists()).toBe(true)

    failing = false
    await wrapper.find('[data-testid="governance-level-retry-agents"]').trigger('click')
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-ready-agents"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="governance-level-error-agents"]').exists()).toBe(false)
  })
})
