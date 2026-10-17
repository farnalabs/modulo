import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'

const { routeState } = vi.hoisted(() => ({
  routeState: { query: {} as Record<string, unknown>, path: '/evidence' },
}))

vi.mock('vue-router', () => ({
  useRoute: () => routeState,
}))

vi.mock('../lib/api/client', () => ({
  getAuthHeaders: () => ({ Authorization: 'Bearer test-token' }),
}))

import EvidenceView from '../views/EvidenceView.vue'

interface Row {
  id: string
  organisation_id: string
  key: string
  subject_type: string
  subject_id: string
  value: unknown
  observed_at: string
  producer_type: string
  producer_id: string | null
  created_at: string
  updated_at: string
}

function row(over: Partial<Row> = {}): Row {
  return {
    id: 'ev-1',
    organisation_id: 'org-1',
    key: 'eval.guardrail',
    subject_type: 'run',
    subject_id: 'run-1',
    value: { raw_output: '[REDACTED]', status: 'complete' },
    observed_at: '2026-01-01T00:00:00Z',
    producer_type: 'eval',
    producer_id: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

function response(payload: unknown, ok = true, status = 200) {
  return { ok, status, json: () => Promise.resolve(payload) }
}

function mountView() {
  return mount(EvidenceView)
}

describe('EvidenceView', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    routeState.query = {}
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the form and the pre-load prompt without fetching', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('Evidence')
    expect(wrapper.find('[data-testid="evidence-run-id"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="evidence-node-id"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="evidence-key"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="evidence-load"]').text()).toBe('Load evidence')
    expect(wrapper.text()).toContain('Choose a run')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects a submit with no run id before calling the API', async () => {
    const wrapper = mountView()

    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Enter a run ID to load its evidence.')
    expect(wrapper.text()).not.toContain('Choose a run')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('sends run, node and key filters and renders the redacted rows as-is', async () => {
    fetchMock.mockResolvedValue(response([row()]))
    const wrapper = mountView()

    await wrapper.find('[data-testid="evidence-run-id"]').setValue('run-1')
    await wrapper.find('[data-testid="evidence-node-id"]').setValue('node-1')
    await wrapper.find('[data-testid="evidence-key"]').setValue('eval.guardrail')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/evidence?run_id=run-1&node_id=node-1&key=eval.guardrail',
    )
    expect(fetchMock.mock.calls[0][1]).toEqual({
      headers: { Authorization: 'Bearer test-token' },
    })
    expect(wrapper.find('[data-testid="evidence-count"]').text()).toBe('1 row')
    expect(wrapper.text()).toContain('eval.guardrail')
    expect(wrapper.text()).toContain('run')
    // The value is rendered exactly as returned (server-side redaction).
    expect(wrapper.text()).toContain('"[REDACTED]"')
    expect(wrapper.text()).toContain('"status": "complete"')
  })

  it('omits the optional filters when they are blank', async () => {
    fetchMock.mockResolvedValue(response([row()]))
    const wrapper = mountView()

    await wrapper.find('[data-testid="evidence-run-id"]').setValue('run-9')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/evidence?run_id=run-9')
  })

  it('shows the loading status while the request is in flight', async () => {
    let resolveFetch!: (value: unknown) => void
    const pending = new Promise((resolve) => {
      resolveFetch = resolve
    })
    fetchMock.mockReturnValue(pending)
    const wrapper = mountView()

    await wrapper.find('[data-testid="evidence-run-id"]').setValue('run-1')
    await wrapper.find('form').trigger('submit')
    await nextTick()

    expect(wrapper.find('[data-testid="evidence-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="evidence-loading"]').attributes('role')).toBe('status')

    resolveFetch(response([row()]))
    await flushPromises()

    expect(wrapper.find('[data-testid="evidence-loading"]').exists()).toBe(false)
  })

  it('renders the empty state when the run has no evidence', async () => {
    fetchMock.mockResolvedValue(response([]))
    const wrapper = mountView()

    await wrapper.find('[data-testid="evidence-run-id"]').setValue('run-1')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('No evidence')
    expect(wrapper.text()).toContain('No evidence rows were recorded for this run.')
    expect(wrapper.find('[data-testid="evidence-count"]').exists()).toBe(false)
  })

  it('surfaces the server problem detail on a failed request', async () => {
    fetchMock.mockResolvedValue(
      response(
        {
          type: 'urn:problem:modulo:validation_error',
          title: 'Validation Error',
          status: 422,
          detail: 'run_id must be a UUID',
        },
        false,
        422,
      ),
    )
    const wrapper = mountView()

    await wrapper.find('[data-testid="evidence-run-id"]').setValue('not-a-uuid')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('run_id must be a UUID')
  })

  it('falls back to the status message when the error body has no detail', async () => {
    fetchMock.mockResolvedValue(response('', false, 500))
    const wrapper = mountView()

    await wrapper.find('[data-testid="evidence-run-id"]').setValue('run-1')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Could not load evidence (HTTP 500).')
  })

  it('treats an unparseable error body as unknown rather than throwing', async () => {
    fetchMock.mockResolvedValue({
      ok: false,
      status: 502,
      json: () => Promise.reject(new Error('invalid json')),
    })
    const wrapper = mountView()

    await wrapper.find('[data-testid="evidence-run-id"]').setValue('run-1')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Unknown error')
  })

  it('renders a rejected fetch message and retries through the error alert', async () => {
    fetchMock
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(response([row()]))
    const wrapper = mountView()

    await wrapper.find('[data-testid="evidence-run-id"]').setValue('run-1')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('network down')

    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/evidence?run_id=run-1')
    expect(wrapper.find('[data-testid="evidence-count"]').text()).toBe('1 row')
  })

  it('stringifies a non-Error rejection', async () => {
    fetchMock.mockRejectedValue('socket closed')
    const wrapper = mountView()

    await wrapper.find('[data-testid="evidence-run-id"]').setValue('run-1')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('socket closed')
  })

  it('formats string, undefined and circular values without throwing', async () => {
    const circular: Record<string, unknown> = {}
    circular.self = circular
    fetchMock.mockResolvedValue(
      response([
        row({ id: 'ev-string', value: 'plain text' }),
        row({ id: 'ev-undefined', value: undefined }),
        row({ id: 'ev-circular', value: circular }),
      ]),
    )
    const wrapper = mountView()

    await wrapper.find('[data-testid="evidence-run-id"]').setValue('run-1')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    const cells = wrapper.findAll('pre')
    expect(cells).toHaveLength(3)
    expect(cells[0].text()).toBe('plain text')
    expect(cells[1].text()).toBe('undefined')
    expect(cells[2].text()).toBe('[object Object]')
  })

  it('auto-loads from string query params on mount', async () => {
    routeState.query = { run_id: 'run-q', node_id: 'node-q', key: 'eval.q' }
    fetchMock.mockResolvedValue(response([row()]))

    const wrapper = mountView()
    await flushPromises()

    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/evidence?run_id=run-q&node_id=node-q&key=eval.q',
    )
    expect((wrapper.find('[data-testid="evidence-run-id"]').element as HTMLInputElement).value).toBe(
      'run-q',
    )
    expect((wrapper.find('[data-testid="evidence-node-id"]').element as HTMLInputElement).value).toBe(
      'node-q',
    )
    expect((wrapper.find('[data-testid="evidence-key"]').element as HTMLInputElement).value).toBe(
      'eval.q',
    )
    expect(wrapper.find('[data-testid="evidence-count"]').text()).toBe('1 row')
  })

  it('ignores non-string node and key query params but still loads the run', async () => {
    routeState.query = { run_id: 'run-q', node_id: ['a', 'b'], key: ['x'] }
    fetchMock.mockResolvedValue(response([row()]))

    const wrapper = mountView()
    await flushPromises()

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/evidence?run_id=run-q')
    expect((wrapper.find('[data-testid="evidence-node-id"]').element as HTMLInputElement).value).toBe('')
    expect((wrapper.find('[data-testid="evidence-key"]').element as HTMLInputElement).value).toBe('')
  })

  it('does not auto-load when the run query param is not a string', async () => {
    routeState.query = { run_id: ['a', 'b'] }

    const wrapper = mountView()
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Choose a run')
  })

  it('does not auto-load when the run query param is an empty string', async () => {
    routeState.query = { run_id: '' }

    const wrapper = mountView()
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Choose a run')
  })
})
