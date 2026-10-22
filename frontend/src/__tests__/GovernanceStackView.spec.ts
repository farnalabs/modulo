import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick as vueNextTick } from 'vue'

async function nextTick() {
  await vueNextTick()
  await flushPromises()
}

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))

/** A page of pipelines as `GET /api/v1/pipelines` returns it. */
function pipelinePage(
  items: Array<{ earned?: string | null; pinned?: string | null }>,
  total: number,
  nextCursor: string | null,
) {
  return ok({
    items: items.map((p) => ({
      earned_autonomy_level: p.earned ?? null,
      default_autonomy_level: p.pinned ?? null,
    })),
    total,
    page: 1,
    page_size: 100,
    next_cursor: nextCursor,
    has_more: nextCursor !== null,
  })
}

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
  mockGet.mockImplementation(async (url: string, ...rest: unknown[]) => {
    const override = overrides[url]
    if (override !== undefined) {
      return typeof override === 'function' ? (override as (...args: unknown[]) => unknown)(url, ...rest) : override
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

    // Exact text, not `toContain`: a pipe-joined message that never
    // pluralised ("2 model backend | 2 model backends") contains every one of
    // these substrings and would pass a containment assertion.
    const count = (id: string) => wrapper.find(`[data-testid="governance-level-count-${id}"]`).text()
    expect(count('models')).toBe('2 model backends')
    expect(count('tools')).toBe('0 connectors')
    expect(count('mcp')).toBe('2 MCP clients or keys')
    expect(count('agents')).toBe('3 agents')
    expect(count('commands')).toBe('9 reusable units')
    expect(count('autonomy')).toBe('4 pipelines')
    expect(count('telemetry')).toBe('4 telemetry destinations')

    expect(wrapper.find('[data-testid="governance-level-state-models"]').text()).toBe('Configured')
    expect(wrapper.find('[data-testid="governance-level-state-tools"]').text()).toBe('Empty')
  })

  it('pluralises a single entity onto the singular form', async () => {
    mockDefaults({
      '/api/v1/model-backends': ok({ items: [{ id: 'm1' }], total: 1, page: 1, page_size: 1 }),
      '/api/v1/agents': ok({ items: [{ id: 'agent-1' }], total: 1, page: 1, page_size: 1 }),
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-count-models"]').text()).toBe('1 model backend')
    expect(wrapper.find('[data-testid="governance-level-count-agents"]').text()).toBe('1 agent')
  })

  it('renders the empty state when a level has no configured entities', async () => {
    mockDefaults({
      '/api/v1/model-backends': ok({ items: [], total: 0, page: 1, page_size: 1 }),
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-state-models"]').text()).toBe('Empty')
    expect(wrapper.find('[data-testid="governance-level-count-models"]').text()).toBe('0 model backends')
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

  it('counts every rung across cursor pages, not only the first page', async () => {
    // 150 pipelines: the first 100-item page holds 50 autonomous + 50 manual,
    // the second page holds the 50 notify pipelines. A single-page read would
    // report 150 pipelines alongside a distribution that sums to 100.
    const pageOne = pipelinePage(
      [
        ...Array.from({ length: 50 }, () => ({ earned: 'fully_autonomous' })),
        ...Array.from({ length: 50 }, () => ({ pinned: 'manual_approval' })),
      ],
      150,
      'cursor-page-2',
    )
    const pageTwo = pipelinePage(
      Array.from({ length: 50 }, () => ({ earned: 'notify_on_complete' })),
      150,
      null,
    )
    mockDefaults({
      '/api/v1/pipelines': (_url: string, options?: { params?: { query?: { cursor?: string } } }) =>
        options?.params?.query?.cursor ? pageTwo : pageOne,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-count-autonomy"]').text()).toBe('150 pipelines')
    const detail = wrapper.find('[data-testid="governance-level-detail-autonomy"]').text()
    expect(detail).toContain('Manual approval: 50')
    expect(detail).toContain('Notify on complete: 50')
    expect(detail).toContain('Fully autonomous: 50')
    expect(detail).not.toContain('of 150 pipelines')
  })

  it('labels the rung distribution as partial when the page walk hits its cap', async () => {
    // Every page claims another page follows; the walk is bounded, so the
    // readout must say it is describing a prefix of the pipeline set rather
    // than silently reporting a complete-looking distribution.
    const page = (_url: string, options?: { params?: { query?: { cursor?: string } } }) => {
      const cursor = options?.params?.query?.cursor
      return pipelinePage([{ pinned: 'manual_approval' }], 5000, cursor ? `cursor-${cursor}` : 'cursor-1')
    }
    mockDefaults({ '/api/v1/pipelines': page })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-count-autonomy"]').text()).toBe('5000 pipelines')
    const detail = wrapper.find('[data-testid="governance-level-detail-autonomy"]').text()
    expect(detail).toContain('Manual approval: 20')
    expect(detail).toContain('of 5000 pipelines')
  })

  it('fails the level when a page after the first fails', async () => {
    const page = (_url: string, options?: { params?: { query?: { cursor?: string } } }) =>
      options?.params?.query?.cursor ? FAILURE : pipelinePage([{ pinned: 'manual_approval' }], 150, 'cursor-page-2')
    mockDefaults({ '/api/v1/pipelines': page })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(errored(wrapper, 'autonomy')).toBe(true)
    expect(restricted(wrapper, 'autonomy')).toBe(false)
    expect(wrapper.find('[data-testid="governance-level-ready-autonomy"]').exists()).toBe(false)
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

  it('groups the restricted notice natively, keeping the level name as its accessible name', async () => {
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

    const block = wrapper.find('[data-testid="governance-level-restricted-tools"]')
    // Web:S6819 - the grouping is a native <fieldset>, not a div with
    // role="group". The visually-hidden legend carries the raw level name, so
    // the group is still announced as "Tools" without a second visible heading.
    expect(block.element.tagName).toBe('FIELDSET')
    expect(block.attributes('role')).toBeUndefined()
    const legend = block.find('legend')
    expect(legend.exists()).toBe(true)
    expect(legend.classes()).toContain('sr-only')
    expect(legend.text()).toBe('Tools')
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

const FORBIDDEN = {
  data: undefined,
  error: { status: 403, detail: 'forbidden' },
  response: { status: 403 },
}
const FEATURE_GATED = {
  data: undefined,
  error: { status: 402, detail: 'feature required' },
  response: { status: 402 },
}
const FAILURE = { data: undefined, error: { detail: 'boom' }, response: { status: 500 } }
const EMPTY = { data: undefined, error: undefined, response: { status: 200 } }

const PRIMARY_URLS = [
  '/api/v1/model-backends',
  '/api/v1/connectors',
  '/api/v1/mcp/oauth/clients',
  '/api/v1/agents',
  '/api/v1/composite-templates',
  '/api/v1/pipelines',
  '/api/v1/settings/observability',
] as const

const ALL_URLS = [
  ...PRIMARY_URLS,
  '/api/v1/api-keys',
  '/api/v1/libraries',
  '/api/v1/errors',
  '/api/v1/notifications',
] as const

function overridesFor(urls: readonly string[], value: unknown): Record<string, unknown> {
  return Object.fromEntries(urls.map((url) => [url, value]))
}

function restricted(wrapper: ReturnType<typeof mountView>, id: string) {
  return wrapper.find(`[data-testid="governance-level-restricted-${id}"]`).exists()
}

function errored(wrapper: ReturnType<typeof mountView>, id: string) {
  return wrapper.find(`[data-testid="governance-level-error-${id}"]`).exists()
}

describe('GovernanceStackView — degraded responses', () => {
  it('marks every level restricted when its primary endpoint is forbidden', async () => {
    mockDefaults(overridesFor(PRIMARY_URLS, FORBIDDEN))
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    for (const id of LEVEL_IDS) {
      expect(restricted(wrapper, id)).toBe(true)
    }
  })

  it('treats a restricted secondary endpoint as restricted for that level only', async () => {
    mockDefaults({
      '/api/v1/api-keys': FORBIDDEN,
      '/api/v1/libraries': FEATURE_GATED,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(restricted(wrapper, 'mcp')).toBe(true)
    expect(restricted(wrapper, 'commands')).toBe(true)
    expect(restricted(wrapper, 'models')).toBe(false)
    expect(wrapper.find('[data-testid="governance-level-ready-models"]').exists()).toBe(true)
  })

  it('reads a gated status carried only on the error body', async () => {
    mockDefaults({
      '/api/v1/agents': {
        data: undefined,
        error: { status: 402, detail: 'gate' },
        response: { status: 500 },
      },
      '/api/v1/model-backends': {
        data: undefined,
        error: { status: 403, detail: 'gate' },
        response: { status: 500 },
      },
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(restricted(wrapper, 'agents')).toBe(true)
    expect(restricted(wrapper, 'models')).toBe(true)
  })

  it('treats a non-object error value as a plain per-level failure', async () => {
    mockDefaults({
      '/api/v1/connectors': { data: undefined, error: 'oops', response: { status: 500 } },
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(errored(wrapper, 'tools')).toBe(true)
    expect(restricted(wrapper, 'tools')).toBe(false)
  })

  it('shows a per-level error when a primary endpoint fails with a non-gated error', async () => {
    mockDefaults(overridesFor(PRIMARY_URLS, FAILURE))
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    for (const id of LEVEL_IDS) {
      expect(errored(wrapper, id)).toBe(true)
    }
  })

  it('shows a per-level error when a secondary endpoint fails', async () => {
    mockDefaults({ '/api/v1/api-keys': FAILURE, '/api/v1/libraries': FAILURE })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(errored(wrapper, 'mcp')).toBe(true)
    expect(errored(wrapper, 'commands')).toBe(true)
    expect(wrapper.find('[data-testid="governance-level-ready-models"]').exists()).toBe(true)
  })

  it('falls back to zero counts when list payloads omit their totals', async () => {
    mockDefaults(overridesFor(ALL_URLS, EMPTY))
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    for (const id of LEVEL_IDS) {
      expect(wrapper.find(`[data-testid="governance-level-ready-${id}"]`).exists()).toBe(true)
    }
    for (const id of ['models', 'tools', 'mcp', 'agents', 'commands', 'autonomy', 'telemetry']) {
      expect(wrapper.find(`[data-testid="governance-level-count-${id}"]`).text()).toContain('0')
    }
  })

  it('counts telemetry destinations even when OTel export is unconfigured', async () => {
    mockDefaults({
      '/api/v1/settings/observability': ok({
        otlp_endpoint: '',
        otlp_headers: {},
        export_interval_seconds: 30,
        langsmith_enabled: false,
        has_langsmith_api_key: false,
        effective_otlp_endpoint: '',
        env_override_active: false,
      }),
      '/api/v1/errors': ok({ items: [], total: 3, limit: 1, offset: 0 }),
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const detail = wrapper.find('[data-testid="governance-level-detail-telemetry"]').text()
    expect(detail).toContain('not configured')
    expect(detail).toContain('3')
    expect(detail).toContain('2')
  })

  it('degrades telemetry enrichment to unavailable without failing the level', async () => {
    mockDefaults({
      '/api/v1/errors': FORBIDDEN,
      '/api/v1/notifications': FORBIDDEN,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-ready-telemetry"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="governance-level-detail-telemetry"]').text()).toContain(
      'unavailable',
    )
  })

  it('treats a bare restricted status on telemetry enrichment as unavailable', async () => {
    mockDefaults({
      '/api/v1/errors': { data: undefined, error: undefined, response: { status: 403 } },
      '/api/v1/notifications': { data: undefined, error: undefined, response: { status: 403 } },
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-ready-telemetry"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="governance-level-detail-telemetry"]').text()).toContain(
      'unavailable',
    )
  })

  it('reads the autonomy rung from the pinned default when the earned level is null', async () => {
    mockDefaults({
      '/api/v1/pipelines': ok({
        items: [{ earned_autonomy_level: null, default_autonomy_level: null }],
        total: 1,
        page: 1,
        page_size: 100,
        has_more: false,
      }),
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="governance-level-detail-autonomy"]').text()).toContain(
      'Manual approval: 1',
    )
  })

  it('shows a retry-able error when a loader throws synchronously', async () => {
    const impl = mockGet.getMockImplementation()
    mockGet.mockImplementation((url: string) => {
      if (url === '/api/v1/connectors') throw new Error('sync boom')
      return impl ? impl(url) : Promise.resolve(EMPTY)
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const error = wrapper.find('[data-testid="governance-level-error-tools"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('sync boom')
    expect(wrapper.find('[data-testid="governance-level-ready-models"]').exists()).toBe(true)
  })
})
