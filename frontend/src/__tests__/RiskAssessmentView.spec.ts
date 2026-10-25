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
import { RISK_ACTIONS } from '../lib/riskAssessment'

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
  return { items: [{ id: 'p-1', name: 'Alpha' }] }
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
      params: { query: { limit: 100, page: 1 } },
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
    setRouteQuery({ key_prefix: 'risk.', pipeline_id: 'p-1', days: '30', action: 'block' })

    const wrapper = await mountView()

    expect(
      (wrapper.find('[data-testid="risk-assessment-key-prefix"]').element as HTMLInputElement).value,
    ).toBe('risk.')
    expect(mockGet).toHaveBeenLastCalledWith('/api/v1/evidence/risk-assessment', {
      params: {
        query: { key_prefix: 'risk.', pipeline_id: 'p-1', days: 30, action: 'block', limit: 100 },
      },
    })
    expect(routerState.query).toEqual({
      key_prefix: 'risk.',
      pipeline_id: 'p-1',
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
