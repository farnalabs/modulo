import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createI18n } from 'vue-i18n'
import { nextTick } from 'vue'

const mockGet = vi.hoisted(() => vi.fn())
vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: vi.fn().mockReturnValue('token'),
  clearAccessToken: vi.fn(),
}))

const routerState = vi.hoisted(() => ({
  query: {} as Record<string, unknown>,
  replace: vi.fn(async (location: { query?: Record<string, unknown> }) => {
    routerState.query = { ...(location.query ?? {}) }
  }),
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: routerState.query }),
  useRouter: () => ({ replace: routerState.replace }),
  createRouter: vi.fn(),
  createWebHistory: vi.fn(() => ({})),
}))

import RiskAssessmentView from '../views/RiskAssessmentView.vue'
import enUS from '../locales/en-US.js'
import { RISK_ACTIONS, RISK_WINDOW_CHOICES } from '../lib/riskAssessment'

// The wire form the API's `pipeline_id` query parameter parses — deep-link
// fixtures carry a real id so the hydration contract is exercised, not a
// short shape no backend would accept.
const PIPELINE_ID = '0f8d5a2e-6f4b-4c8a-9e1d-3b7c2a5d8f01'

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})

function setRouteQuery(query: Record<string, unknown> = {}): void {
  routerState.query = { ...query }
  routerState.replace.mockClear()
}

function projectionResponse() {
  return {
    summary: {
      total_rows: 1,
      action_counts: { block: 1, warn: 0, continue: 0, escalate: 0 },
      facet_degraded: false,
    },
    rows: [
      {
        id: 'e-1',
        organisation_id: 'o-1',
        key: 'risk.api_call',
        subject_type: 'run',
        subject_id: 'run-1',
        value: { status: 'block' },
        observed_at: '2026-01-01T00:00:00Z',
        producer_type: 'run',
        producer_id: null,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        action: 'block',
      },
    ],
  }
}

function pipelinesResponse() {
  return { items: [{ id: PIPELINE_ID, name: 'Alpha' }] }
}

function ok(data: unknown) {
  return { data, error: undefined, response: { ok: true, status: 200 } }
}

function routeAwareGet() {
  return mockGet.mockImplementation((path: string) => {
    if (path === '/api/v1/pipelines') return Promise.resolve(ok(pipelinesResponse()))
    return Promise.resolve(ok(projectionResponse()))
  })
}

async function mountView() {
  const wrapper = mount(RiskAssessmentView, {
    global: { plugins: [createPinia(), i18n] },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('RiskAssessmentView', () => {
  it('loads the projection and renders the summary and table', async () => {
    routeAwareGet()
    setRouteQuery()

    const wrapper = await mountView()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/pipelines', {
      params: { query: { page: 1, page_size: 100 } },
    })
    expect(mockGet).toHaveBeenCalledWith('/api/v1/evidence/risk-assessment', {
      params: { query: { days: 7, limit: 100 } },
    })
    expect(wrapper.find('[data-testid="risk-assessment-total"]').text()).toBe('1')
    expect(wrapper.findAll('[data-testid="risk-assessment-row"]')).toHaveLength(1)
    expect(wrapper.find('[data-testid="risk-assessment-loading"]').exists()).toBe(false)
  })

  it('hydrates filters from the URL and re-affirms the deep link', async () => {
    routeAwareGet()
    setRouteQuery({ key_prefix: 'risk.', pipeline_id: PIPELINE_ID, days: '30', action: 'block' })

    const wrapper = await mountView()

    expect(
      (wrapper.find('[data-testid="risk-assessment-key-prefix"]').element as HTMLInputElement).value,
    ).toBe('risk.')
    expect(mockGet).toHaveBeenLastCalledWith('/api/v1/evidence/risk-assessment', {
      params: {
        query: { key_prefix: 'risk.', pipeline_id: PIPELINE_ID, days: 30, action: 'block', limit: 100 },
      },
    })
    expect(routerState.query).toEqual({
      key_prefix: 'risk.',
      pipeline_id: PIPELINE_ID,
      days: '30',
      action: 'block',
    })
  })

  it('ignores a non-numeric days query param', async () => {
    routeAwareGet()
    setRouteQuery({ days: 'forever' })

    await mountView()

    expect(mockGet).toHaveBeenLastCalledWith('/api/v1/evidence/risk-assessment', {
      params: { query: { days: 7, limit: 100 } },
    })
  })

  it('drops out-of-contract deep-link values to defaults instead of sending a 422 the retry can never clear', async () => {
    // Each of these values is answered 422 by the backend (days outside the
    // offered windows, an unknown action, a non-UUID pipeline id) — and the
    // error alert's Retry re-applies the same filters, so a hydrated 422 would
    // loop forever. A deep link is written by this surface, so only the facet
    // vocabulary is hydratable; anything else is a malformed link.
    for (const malformed of [
      { days: '400' },
      { days: '-5' },
      { days: '30abc' },
      { action: 'nonsense' },
      { pipeline_id: 'not-a-uuid' },
    ]) {
      mockGet.mockClear()
      routeAwareGet()
      setRouteQuery(malformed)

      await mountView()

      expect(mockGet).toHaveBeenLastCalledWith(
        '/api/v1/evidence/risk-assessment',
        expect.objectContaining({
          params: { query: { days: 7, limit: 100 } },
        }),
      )
    }
  })

  it('keeps a deep link inside the offered time-window vocabulary round-trippable', async () => {
    for (const choice of RISK_WINDOW_CHOICES) {
      mockGet.mockClear()
      routeAwareGet()
      setRouteQuery({ days: String(choice.value) })

      await mountView()

      expect(mockGet).toHaveBeenLastCalledWith('/api/v1/evidence/risk-assessment', {
        params: { query: { days: choice.value, limit: 100 } },
      })
    }
  })

  it('ignores non-string query params', async () => {
    routeAwareGet()
    setRouteQuery({ days: ['1', '2'] })

    await mountView()

    // A repeated query param arrives as an array, which is not a valid filter.
    expect(mockGet).toHaveBeenLastCalledWith('/api/v1/evidence/risk-assessment', {
      params: { query: { days: 7, limit: 100 } },
    })
  })

  it('writes the applied filters to the URL when the operator applies', async () => {
    routeAwareGet()
    setRouteQuery()
    const wrapper = await mountView()

    await wrapper.find('[data-testid="risk-assessment-key-prefix"]').setValue('risk.')
    await wrapper.find('[data-testid="risk-assessment-action"]').setValue('warn')
    await wrapper.find('[data-testid="risk-assessment-apply"]').trigger('click')
    await flushPromises()

    expect(routerState.query).toEqual({ key_prefix: 'risk.', days: '7', action: 'warn' })
    expect(mockGet).toHaveBeenLastCalledWith('/api/v1/evidence/risk-assessment', {
      params: { query: { key_prefix: 'risk.', days: 7, action: 'warn', limit: 100 } },
    })
  })

  it('resets filters and refetches', async () => {
    routeAwareGet()
    setRouteQuery({ action: 'block' })
    const wrapper = await mountView()

    await wrapper.find('[data-testid="risk-assessment-reset"]').trigger('click')
    await flushPromises()

    expect(routerState.query).toEqual({ days: '7' })
    expect(
      (wrapper.find('[data-testid="risk-assessment-action"]').element as HTMLSelectElement).value,
    ).toBe('')
  })

  it('renders the error alert and retries via the shared apply path', async () => {
    let projectionAttempts = 0
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(ok(pipelinesResponse()))
      projectionAttempts += 1
      if (projectionAttempts === 1) {
        return Promise.resolve({
          data: undefined,
          error: { detail: 'Projection failed' },
          response: { ok: false, status: 500 },
        })
      }
      return Promise.resolve(ok(projectionResponse()))
    })
    setRouteQuery()

    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="risk-assessment-error"]').text()).toContain('Could not load risk assessment. Try again.')
    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    await flushPromises()

    expect(projectionAttempts).toBe(2)
    expect(wrapper.find('[data-testid="risk-assessment-summary-card"]').exists()).toBe(true)
  })

  it('shows the loading region while the projection is in flight', async () => {
    let resolveProjection: (value: unknown) => void = () => {}
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(ok(pipelinesResponse()))
      return new Promise((resolve) => {
        resolveProjection = resolve
      })
    })
    setRouteQuery()
    const wrapper = mount(RiskAssessmentView, {
      global: { plugins: [createPinia(), i18n] },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="risk-assessment-loading"]').exists()).toBe(true)

    resolveProjection(ok(projectionResponse()))
    await flushPromises()
    expect(wrapper.find('[data-testid="risk-assessment-loading"]').exists()).toBe(false)
  })

  it('surfaces a pipeline list failure without blocking the projection', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') {
        return Promise.resolve({
          data: undefined,
          error: undefined,
          response: { ok: false, status: 503 },
        })
      }
      return Promise.resolve(ok(projectionResponse()))
    })
    setRouteQuery()

    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="risk-assessment-pipelines-error-message"]').text()).toBe('Could not load the pipeline list. Try again.')
    expect(wrapper.findAll('[data-testid="risk-assessment-row"]')).toHaveLength(1)
  })

  it('surfaces a pipeline problem detail and tolerates a missing items list', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') {
        return Promise.resolve({
          data: undefined,
          error: { detail: 'Pipelines forbidden' },
          response: { ok: false, status: 403 },
        })
      }
      return Promise.resolve(ok(projectionResponse()))
    })
    setRouteQuery()

    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="risk-assessment-pipelines-error-message"]').text()).toBe(
      'Could not load the pipeline list. Try again.',
    )
    expect(wrapper.findAll('[data-testid="risk-assessment-row"]')).toHaveLength(1)
  })

  it('tolerates a pipeline response without an items array', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') return Promise.resolve(ok({}))
      return Promise.resolve(ok(projectionResponse()))
    })
    setRouteQuery()

    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="risk-assessment-pipelines-error"]').exists()).toBe(false)
  })

  it('reports HTTP 0 when the pipeline request has no response object', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') {
        return Promise.resolve({ data: undefined, error: undefined, response: undefined })
      }
      return Promise.resolve(ok(projectionResponse()))
    })
    setRouteQuery()

    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="risk-assessment-pipelines-error-message"]').text()).toBe('Could not load the pipeline list. Try again.')
  })

  it('walks every pipeline page so the facet can select pipelines beyond the first page', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') {
        const page = mockGet.mock.calls.filter(([p]) => p === '/api/v1/pipelines').length
        if (page === 1) {
          return Promise.resolve(
            ok({
              items: [{ id: 'p-1', name: 'Alpha' }],
              total: 2,
              page: 1,
              page_size: 100,
              next_cursor: null,
              has_more: true,
            }),
          )
        }
        return Promise.resolve(
          ok({
            items: [{ id: 'p-2', name: 'Beta' }],
            total: 2,
            page: 2,
            page_size: 100,
            next_cursor: null,
            has_more: false,
          }),
        )
      }
      return Promise.resolve(ok(projectionResponse()))
    })
    setRouteQuery()

    const wrapper = await mountView()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/pipelines', {
      params: { query: { page: 1, page_size: 100 } },
    })
    expect(mockGet).toHaveBeenCalledWith('/api/v1/pipelines', {
      params: { query: { page: 2, page_size: 100 } },
    })
    const options = wrapper
      .find('[data-testid="risk-assessment-pipeline"]')
      .findAll('option')
      .map((option) => option.text())
    expect(options).toEqual(['All pipelines', 'Alpha', 'Beta'])
  })

  it('never publishes a partial pipeline list when a later page fails', async () => {
    let pipelineCalls = 0
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') {
        pipelineCalls += 1
        if (pipelineCalls === 1) {
          return Promise.resolve(
            ok({
              items: [{ id: 'p-1', name: 'Alpha' }],
              total: 2,
              page: 1,
              page_size: 100,
              next_cursor: null,
              has_more: true,
            }),
          )
        }
        return Promise.resolve({
          data: undefined,
          error: { detail: 'Pipeline page failed' },
          response: { ok: false, status: 500 },
        })
      }
      return Promise.resolve(ok(projectionResponse()))
    })
    setRouteQuery()

    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="risk-assessment-pipelines-error-message"]').text()).toBe(
      'Could not load the pipeline list. Try again.',
    )
    const options = wrapper
      .find('[data-testid="risk-assessment-pipeline"]')
      .findAll('option')
      .map((option) => option.text())
    expect(options).toEqual(['All pipelines'])
  })

  it('labels a truncated pipeline list instead of presenting it as complete', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/pipelines') {
        const page = mockGet.mock.calls.filter(([p]) => p === '/api/v1/pipelines').length
        return Promise.resolve(
          ok({
            items: [{ id: `p-${page}`, name: `Pipeline ${page}` }],
            total: 999,
            page,
            page_size: 100,
            next_cursor: null,
            has_more: true,
          }),
        )
      }
      return Promise.resolve(ok(projectionResponse()))
    })
    setRouteQuery()

    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="risk-assessment-pipelines-error-message"]').text()).toBe(
      'The pipeline list is too large to load in full; some pipelines may not be selectable. Narrow the run history or retry.',
    )
    const options = wrapper
      .find('[data-testid="risk-assessment-pipeline"]')
      .findAll('option')
      .map((option) => option.text())
    // All 20 pages (the bound) were collected before the truncation label.
    expect(options).toHaveLength(21)
    expect(options[20]).toBe('Pipeline 20')
  })

  it('exposes every gate action in the facet', async () => {
    routeAwareGet()
    setRouteQuery()

    const wrapper = await mountView()

    const values = wrapper
      .find('[data-testid="risk-assessment-action"]')
      .findAll('option')
      .map((o) => o.attributes('value'))
    expect(values).toEqual(['', ...RISK_ACTIONS])
  })
})
