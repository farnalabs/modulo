import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick as vueNextTick } from 'vue'
import type { Mock } from 'vitest'

async function nextTick() { await vueNextTick(); await flushPromises() }

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminAuditView from '../views/AdminAuditView.vue'
import { api } from '../lib/api/client'

const auditEvent = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  event_type: 'pipeline.created',
  actor_user_id: 'user-12345678',
  created_at: '2026-08-20T10:30:00Z',
  resource_type: 'pipeline',
  resource_id: 'pipe-1',
  payload_json: { name: 'Deploy Pipeline' },
  request_id: 'req-abcdef12',
  previous_hash: 'hash-abcdef99',
  ...over,
})

const pagePayload = (items: unknown[], over: Record<string, unknown> = {}) => ({
  data: { items, total: items.length, next_cursor: null, prev_cursor: null, ...over },
  error: undefined,
})

function mockAuditGet(items: unknown[], over: Record<string, unknown> = {}) {
  ;(api.GET as Mock).mockImplementation(async (url: string) => {
    if (url === '/api/v1/admin/audit') return pagePayload(items, over)
    if (url === '/api/v1/admin/feature-flags') return { data: { license: { tier: 'team' }, flags: [] }, error: undefined }
    if (url === '/api/v1/admin/license') return { data: { tier: 'team' }, error: undefined }
    if (url === '/api/v1/admin/tiers') return { data: { tiers: [{ tier_id: 'team', label: 'Team', rank: 1 }] }, error: undefined }
    return { data: undefined, error: { detail: `unrouted: ${url}` } }
  })
}

function mountView() {
  return mount(AdminAuditView, {
    global: {
      stubs: {
        FeatureGate: { template: '<div><slot /></div>' },
        JsonViewer: { props: ['data', 'showToolbar', 'maxHeight'], template: '<div data-testid="json-viewer-stub" />' },
      },
    },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  mockAuditGet([auditEvent('evt-1'), auditEvent('evt-2', { event_type: 'run.failed' })])
})

// The initial fetch succeeds (the filter refs are declared above
// useDataFetch, FAR-608). With auto-apply (FAR-868), filters are applied
// automatically on change — no explicit click needed.
async function mountLoaded() {
  const wrapper = mountView()
  await nextTick()
  return wrapper
}

describe('AdminAuditView — event list', () => {
  it('loads the audit events on mount without a TDZ error (FAR-608 fix)', async () => {
    // The filter refs are declared above the useDataFetch call, so
    // buildQuery() no longer hits a temporal dead zone on the initial fetch.
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.text()).not.toContain("Cannot access 'filterEventType' before initialization")
    expect(wrapper.find('[data-testid="admin-audit-event-row-evt-1"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-audit-event-row-evt-2"]').exists()).toBe(true)
  })

  it('renders audit events with timestamp, badge, actor, target and summary', async () => {
    const wrapper = await mountLoaded()

    expect(wrapper.find('[data-testid="admin-audit-event-row-evt-1"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('pipeline.created')
    expect(wrapper.text()).toContain('usr_user-123') // formatActor: usr_ + shortId
    expect(wrapper.text()).toContain('pipeline / #pipe-1') // resource id rendered via shortId
    expect(wrapper.text()).toContain('Created pipeline "Deploy Pipeline"')
    expect(wrapper.find('[data-testid="admin-audit-event-row-evt-2"]').exists()).toBe(true)
    // events count line
    expect(wrapper.text()).toContain('2 events')
  })

  it('renders the semantic actor and summary the backend composed (FAR-728)', async () => {
    mockAuditGet([
      auditEvent('evt-sem', {
        actor_user_id: null,
        payload_json: {
          actor: 'cron trigger (a1b2c3d4)',
          summary: 'Pipeline "PR Reviewer Agent" (d6b2c25b) run triggered by cron',
        },
      }),
    ])
    const wrapper = await mountLoaded()

    expect(wrapper.text()).toContain('cron trigger (a1b2c3d4)')
    expect(wrapper.text()).toContain('Pipeline "PR Reviewer Agent" (d6b2c25b) run triggered by cron')
    // The legacy heuristic must NOT win over the backend-provided summary.
    expect(wrapper.text()).not.toContain('Created pipeline')
  })

  it('falls back to usr_ rendering for user events without a semantic actor', async () => {
    mockAuditGet([auditEvent('evt-user', { actor_user_id: 'user-12345678', payload_json: { summary: 'X' } })])
    const wrapper = await mountLoaded()

    expect(wrapper.text()).toContain('usr_user-123')
    expect(wrapper.text()).toContain('X')
  })

  it('applies the destructive badge class to run.failed events', async () => {
    const wrapper = await mountLoaded()
    const badge = wrapper.find('[data-testid="admin-audit-event-row-evt-2"] .badge')
    expect(badge.classes()).toContain('badge-status-destructive')
  })

  it('shows the empty state when no events are returned (fe-003)', async () => {
    mockAuditGet([])
    const wrapper = await mountLoaded()
    expect(wrapper.text()).toContain('No audit events found')
    expect(wrapper.find('[data-testid="admin-audit-event-row-evt-1"]').exists()).toBe(false)
  })

  it('shows the skeleton table while loading', async () => {
    ;(api.GET as Mock).mockImplementation(() => new Promise(() => {}))
    const wrapper = mountView()
    await vueNextTick()
    expect(wrapper.find('[data-testid="admin-audit-event-row-evt-1"]').exists()).toBe(false)
    // The loading skeleton table renders 8 placeholder rows.
    expect(wrapper.findAll('tbody tr')).toHaveLength(8)
  })

  it('the ErrorAlert retry button renders (fe-002, FAR-608 fix)', async () => {
    // ErrorAlert defaults retryable to true, so a load failure with an
    // on-retry handler offers the retry action.
    // Load first, then make the refetch fail.
    const wrapper = await mountLoaded()
    ;(api.GET as Mock).mockRejectedValue(new Error('audit down'))
    // Trigger a refetch via the reset button (auto-apply means filter changes trigger refetch)
    await wrapper.find('[data-testid="admin-audit-reset"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('audit down')
    expect(wrapper.findAll('button').filter((b) => b.text() === 'Retry')).toHaveLength(1)
  })
})

describe('AdminAuditView — row expansion', () => {
  it('expands a row on click and shows payload viewer, hashes and request id', async () => {
    const wrapper = await mountLoaded()

    await wrapper.find('[data-testid="admin-audit-event-row-evt-1"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="json-viewer-stub"]').exists()).toBe(true)
    // shortId renders '#'+first 8 chars for hash / id / request id.
    expect(wrapper.text()).toContain('#hash-abc')
    expect(wrapper.text()).toContain('#evt-1')
    expect(wrapper.text()).toContain('#req-abcd')

    // Collapse on second click.
    await wrapper.find('[data-testid="admin-audit-event-row-evt-1"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="json-viewer-stub"]').exists()).toBe(false)
  })

  it('toggles expansion via the row chevron button', async () => {
    const wrapper = await mountLoaded()

    await wrapper.find('[data-testid="admin-audit-event-expand-evt-1"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="json-viewer-stub"]').exists()).toBe(true)

    // Switching to another event moves the expansion.
    await wrapper.find('[data-testid="admin-audit-event-expand-evt-2"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('#evt-2')
  })

  it('toggles row expansion via keyboard (Enter and Space) for a11y', async () => {
    const wrapper = await mountLoaded()
    const row = wrapper.find('[data-testid="admin-audit-event-row-evt-1"]')

    // Enter opens expansion.
    await row.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(wrapper.find('[data-testid="json-viewer-stub"]').exists()).toBe(true)

    // Enter again collapses it.
    await row.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(wrapper.find('[data-testid="json-viewer-stub"]').exists()).toBe(false)

    // Space re-opens expansion (keyboard-only equivalent of the click).
    await row.trigger('keydown', { key: ' ' })
    await nextTick()
    expect(wrapper.find('[data-testid="json-viewer-stub"]').exists()).toBe(true)
  })
})

describe('AdminAuditView — pagination', () => {
  it('disables previous without a cursor and fetches the next page with the cursor', async () => {
    mockAuditGet([auditEvent('evt-2')], { next_cursor: 'cur-2', total: 120 })
    const wrapper = await mountLoaded()

    const prev = wrapper.find('[data-testid="admin-audit-previous"]')
    expect(prev.attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('Page 1')

    await wrapper.find('[data-testid="admin-audit-next"]').trigger('click')
    await nextTick()

    const calls = (api.GET as Mock).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/admin/audit')
    const lastQuery = calls[calls.length - 1][1].params.query
    expect(lastQuery.cursor).toBe('cur-2')
    expect(lastQuery.limit).toBe(50)
    expect(wrapper.text()).toContain('Page 2')
  })

  it('goes back with the previous cursor and decrements the page counter', async () => {
    mockAuditGet([auditEvent('evt-2')], { next_cursor: 'cur-2', prev_cursor: 'cur-0', total: 120 })
    const wrapper = await mountLoaded()
    expect(wrapper.text()).toContain('Page 1')

    await wrapper.find('[data-testid="admin-audit-next"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Page 2')

    mockAuditGet([auditEvent('evt-1')], { prev_cursor: null, total: 120 })
    await wrapper.find('[data-testid="admin-audit-previous"]').trigger('click')
    await nextTick()
    const calls = (api.GET as Mock).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/admin/audit')
    expect(calls[calls.length - 1][1].params.query.cursor).toBe('cur-0')
    expect(wrapper.text()).toContain('Page 1')
  })
})

describe('AdminAuditView — filters', () => {
  it('renders the To-date and target-type filters (FilterBar default slot, FAR-608 fix)', async () => {
    // FilterBar now renders its default slot, so the view's To date input and
    // target-type Select are live UI.
    const wrapper = await mountLoaded()
    expect(wrapper.find('[data-testid="admin-audit-date-to"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="admin-audit-target-type"]').exists()).toBe(true)
  })

  it('auto-applies filter changes — sets actor, from-date and entity type (FAR-868)', async () => {
    mockAuditGet([auditEvent('evt-1')], { total: 3 })
    const wrapper = await mountLoaded()

    await wrapper.find('[data-testid="admin-audit-actor"]').setValue('user-12345678')
    await wrapper.find('[data-testid="admin-audit-date-from"]').setValue('2026-08-01')
    await wrapper.find('[data-testid="admin-audit-date-to"]').setValue('2026-08-31')
    // The target-type filter is a PrimeVue Select — drive its ref directly.
    const vm = wrapper.vm as unknown as { filterTargetType: string }
    vm.filterTargetType = 'pipeline'
    await nextTick()

    // Wait for debounce (300ms) on text/date filters
    await new Promise(r => setTimeout(r, 400))
    await nextTick()

    const calls = (api.GET as Mock).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/admin/audit')
    const q = calls[calls.length - 1][1].params.query
    expect(q.user_id).toBe('user-12345678')
    expect(q.from_date).toBe('2026-08-01')
    expect(q.to_date).toBe('2026-08-31')
    expect(q.entity_type).toBe('pipeline')
    expect(q.cursor).toBeUndefined()
    expect(q.limit).toBe(50)
  })

  it('reset clears every filter and refetches without filter params', async () => {
    mockAuditGet([auditEvent('evt-1')])
    const wrapper = await mountLoaded()

    await wrapper.find('[data-testid="admin-audit-actor"]').setValue('user-x')
    const vm = wrapper.vm as unknown as { filterTargetType: string }
    vm.filterTargetType = 'run'
    await nextTick()

    await wrapper.find('[data-testid="admin-audit-reset"]').trigger('click')
    await nextTick()

    expect(vm.filterTargetType).toBe('__all__')
    expect((wrapper.find('[data-testid="admin-audit-actor"]').element as HTMLInputElement).value).toBe('')
    const calls = (api.GET as Mock).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/admin/audit')
    const q = calls[calls.length - 1][1].params.query
    expect(q.user_id).toBeUndefined()
    expect(q.entity_type).toBeUndefined()
  })
})

describe('AdminAuditView — verify chain', () => {
  it('shows the valid chain result with the verified event count', async () => {
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/admin/audit/verify') {
        return { data: { valid: true, event_count: 2 }, error: undefined }
      }
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="admin-audit-verify-chain"]').trigger('click')
    await nextTick()

    const result = wrapper.find('[data-testid="admin-audit-chain-result"]')
    expect(result.exists()).toBe(true)
    expect(result.text()).toContain('Chain Integrity: ✅ Valid')
    expect(result.text()).toContain('2 events verified')
  })

  it('shows the broken chain result with the detail message', async () => {
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/admin/audit/verify') {
        return {
          data: {
            valid: false,
            event_count: 1,
            detail: 'Audit chain break at event 1 (id evt-2): stored previous_hash (bad-hash) does not match',
          },
          error: undefined,
        }
      }
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="admin-audit-verify-chain"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="admin-audit-chain-result"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Chain Integrity: ❌ Broken')
    expect(wrapper.text()).toContain('bad-hash')
  })

  it('shows the error envelope as a broken chain with the formatted detail (FAR-608 fix)', async () => {
    // verifyChain formats the error envelope via formatError, so the detail
    // message renders instead of '[object Object]'.
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/admin/audit/verify') {
        return { data: undefined, error: { detail: 'verify exploded' } }
      }
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="admin-audit-verify-chain"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Chain Integrity: ❌ Broken')
    expect(wrapper.text()).toContain('verify exploded')
  })
})

describe('AdminAuditView — exports', () => {
  let clickSpy: ReturnType<typeof vi.fn>
  let createObjectURLSpy: ReturnType<typeof vi.fn>
  let revokeObjectURLSpy: ReturnType<typeof vi.fn>
  const originalCreateElement = document.createElement.bind(document)

  beforeEach(() => {
    clickSpy = vi.fn()
    createObjectURLSpy = vi.fn().mockReturnValue('blob:mock-url')
    revokeObjectURLSpy = vi.fn()
    URL.createObjectURL = createObjectURLSpy as unknown as typeof URL.createObjectURL
    URL.revokeObjectURL = revokeObjectURLSpy as unknown as typeof URL.revokeObjectURL
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
      if (tag === 'a') {
        return { href: '', download: '', click: clickSpy } as unknown as HTMLAnchorElement
      }
      return originalCreateElement(tag)
    }) as unknown as typeof document.createElement)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('export CSV downloads a CSV blob built from every export page', async () => {
    ;(api.GET as Mock).mockImplementation(async (url: string, opts?: { params?: { query?: { page?: number } } }) => {
      if (url === '/api/v1/admin/audit/export') {
        const page = opts?.params?.query?.page ?? 1
        return pagePayload([auditEvent(`export-${page}`)], { total: 120 })
      }
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="admin-audit-export-csv"]').trigger('click')
    await nextTick()
    await flushPromises()

    // total 120 > page_size 1000? No — a single page is fetched.
    const exportCalls = (api.GET as Mock).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/admin/audit/export')
    expect(exportCalls).toHaveLength(1)
    expect(exportCalls[0][1].params.query.page_size).toBe(1000)

    expect(clickSpy).toHaveBeenCalledTimes(1)
    expect(revokeObjectURLSpy).toHaveBeenCalledWith('blob:mock-url')
    const blob = createObjectURLSpy.mock.calls[0][0] as Blob
    const csv = await blob.text()
    expect(csv).toContain('Timestamp,Event Type,Actor ID,Target Type,Target ID,Summary,Request ID,Previous Hash')
    expect(csv).toContain('pipeline.created')
    expect(csv).toContain('"Created pipeline ""Deploy Pipeline"""')
  })

  it('export CSV paginates until total pages are consumed', async () => {
    ;(api.GET as Mock).mockImplementation(async (url: string, opts?: { params?: { query?: { page?: number } } }) => {
      if (url === '/api/v1/admin/audit/export') {
        const page = opts?.params?.query?.page ?? 1
        return pagePayload([auditEvent(`export-${page}`)], { total: 1500 })
      }
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="admin-audit-export-csv"]').trigger('click')
    await nextTick()
    await flushPromises()

    const exportCalls = (api.GET as Mock).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/admin/audit/export')
    expect(exportCalls).toHaveLength(2)
    expect(exportCalls[1][1].params.query.page).toBe(2)
  })

  it('export CSV surfaces the failure from the error envelope', async () => {
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/admin/audit/export') {
        return { data: undefined, error: { detail: 'export denied' } }
      }
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="admin-audit-export-csv"]').trigger('click')
    await nextTick()
    await flushPromises()
    expect(wrapper.text()).toContain('Export failed:')
    expect(wrapper.text()).toContain('export denied')
    expect(clickSpy).not.toHaveBeenCalled()
  })

  it('export JSONL downloads one JSON object per line', async () => {
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/admin/audit/export') {
        return pagePayload([auditEvent('j-1'), auditEvent('j-2')], { total: 2 })
      }
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="admin-audit-export-jsonl"]').trigger('click')
    await nextTick()
    await flushPromises()

    expect(clickSpy).toHaveBeenCalledTimes(1)
    const blob = createObjectURLSpy.mock.calls[0][0] as Blob
    const jsonl = await blob.text()
    const lines = jsonl.split('\n')
    expect(lines).toHaveLength(2)
    expect(JSON.parse(lines[0])).toMatchObject({ id: 'j-1', event_type: 'pipeline.created' })
    expect(JSON.parse(lines[1])).toMatchObject({ id: 'j-2' })
  })

  it('export JSONL surfaces the failure from the error envelope', async () => {
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/admin/audit/export') {
        return { data: undefined, error: { detail: 'jsonl denied' } }
      }
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()

    await wrapper.find('[data-testid="admin-audit-export-jsonl"]').trigger('click')
    await nextTick()
    await flushPromises()
    expect(wrapper.text()).toContain('Export failed:')
    expect(wrapper.text()).toContain('jsonl denied')
    expect(clickSpy).not.toHaveBeenCalled()
  })

  it('expands an audit row via keyboard (Enter / Space) for a11y (FAR-821)', async () => {
    const wrapper = await mountLoaded()
    const row = wrapper.find('[data-testid="admin-audit-event-row-evt-1"]')
    expect(row.exists()).toBe(true)

    await row.trigger('keydown', { key: 'Enter' })
    await nextTick()
    expect(wrapper.find('[data-testid="json-viewer-stub"]').exists()).toBe(true)

    await row.trigger('keydown', { key: ' ', code: 'Space' })
    await nextTick()
    expect(wrapper.find('[data-testid="json-viewer-stub"]').exists()).toBe(false)
  })
})

// ---- Branch coverage: formatActor various paths ----
describe('AdminAuditView — formatActor branches', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders a dash when actor_user_id is null and no actor label', async () => {
    mockAuditGet([auditEvent('evt-no-actor', { actor_user_id: null, payload_json: {} })])
    const wrapper = await mountLoaded()
    const cell = wrapper.find('[data-testid="admin-audit-event-row-evt-no-actor"]')
    expect(cell.text()).toContain('—')
  })

  it('renders the semantic actor label from payload_json', async () => {
    mockAuditGet([auditEvent('evt-sys', { actor_user_id: null, payload_json: { actor: 'system' } })])
    const wrapper = await mountLoaded()
    expect(wrapper.text()).toContain('system')
  })
})

// ---- Branch coverage: summarize various paths ----
describe('AdminAuditView — summarize branches', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('uses the backend summary when present', async () => {
    mockAuditGet([auditEvent('evt-sum', { payload_json: { summary: 'Custom summary' } })])
    const wrapper = await mountLoaded()
    expect(wrapper.text()).toContain('Custom summary')
  })

  it('falls back to heuristic when no backend summary', async () => {
    mockAuditGet([auditEvent('evt-heur', { event_type: 'pipeline.created', resource_type: 'pipeline', payload_json: { name: 'My Pipeline' } })])
    const wrapper = await mountLoaded()
    expect(wrapper.text()).toContain('Created pipeline')
    expect(wrapper.text()).toContain('"My Pipeline"')
  })

  it('falls back to heuristic with no name or display_name', async () => {
    mockAuditGet([auditEvent('evt-noname', { event_type: 'user.created', resource_type: 'user', payload_json: {} })])
    const wrapper = await mountLoaded()
    expect(wrapper.text()).toContain('Created user')
  })

  it('falls back to heuristic when event_type has no dot', async () => {
    mockAuditGet([auditEvent('evt-nodot', { event_type: 'customaction', resource_type: 'thing', payload_json: {} })])
    const wrapper = await mountLoaded()
    expect(wrapper.text()).toContain('Customaction thing')
  })
})

// ---- Branch coverage: badgeClass various event types ----
describe('AdminAuditView — badgeClass branches', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('applies the correct badge class for each event type prefix', async () => {
    const eventTypes = [
      { type: 'pipeline.created', expected: 'badge-context-blue' },
      { type: 'run.completed', expected: 'badge-status-success' },
      { type: 'run.failed', expected: 'badge-status-destructive' },
      { type: 'run.cancelled', expected: 'badge-status-warning' },
      { type: 'user.created', expected: 'badge-context-purple' },
      { type: 'team.created', expected: 'badge-context-indigo' },
      { type: 'schema.created', expected: 'badge-context-cyan' },
      { type: 'connector.created', expected: 'badge-context-orange' },
      { type: 'model_backend.created', expected: 'badge-context-pink' },
      { type: 'sso_provider.created', expected: 'badge-context-slate' },
      { type: 'settings.updated', expected: 'badge-context-slate' },
      { type: 'api_key.created', expected: 'badge-context-rose' },
      { type: 'export.csv', expected: 'badge-context-blue' },
    ]
    for (const { type, expected } of eventTypes) {
      mockAuditGet([auditEvent('evt-' + type.replace('.', '-'), { event_type: type })])
      const wrapper = mountView()
      await nextTick()
      const badge = wrapper.find('.badge')
      expect(badge.classes()).toContain(expected)
      wrapper.unmount()
    }
  })
})

// ---- Branch coverage: formatTimestamp null ----
describe('AdminAuditView — formatTimestamp null', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders a dash for null timestamp', async () => {
    mockAuditGet([auditEvent('evt-null-ts', { created_at: null })])
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.text()).toContain('—')
  })
})

// ---- Branch coverage: expanded row conditional branches ----
describe('AdminAuditView — expanded row conditional branches', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('hides details when payload_json is empty', async () => {
    mockAuditGet([auditEvent('evt-empty-payload', { payload_json: {} })])
    const wrapper = await mountView()
    await nextTick()
    await wrapper.find('[data-testid="admin-audit-event-row-evt-empty-payload"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="json-viewer-stub"]').exists()).toBe(false)
  })

  it('hides previous_hash and request_id sections when absent', async () => {
    mockAuditGet([auditEvent('evt-no-hash', { previous_hash: null, request_id: null })])
    const wrapper = await mountView()
    await nextTick()
    await wrapper.find('[data-testid="admin-audit-event-row-evt-no-hash"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).not.toContain('Previous Hash')
    expect(wrapper.text()).not.toContain('Request ID')
  })

  it('shows previous_hash and request_id when present', async () => {
    mockAuditGet([auditEvent('evt-with-hash', { previous_hash: 'abc12345', request_id: 'req-999' })])
    const wrapper = await mountView()
    await nextTick()
    await wrapper.find('[data-testid="admin-audit-event-row-evt-with-hash"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Previous Hash')
    expect(wrapper.text()).toContain('Request ID')
    expect(wrapper.text()).toContain('#abc12345')
  })

  it('hides resource_id when absent', async () => {
    mockAuditGet([auditEvent('evt-no-res', { resource_id: null })])
    const wrapper = mountView()
    await nextTick()
    const cell = wrapper.find('[data-testid="admin-audit-event-row-evt-no-res"]')
    expect(cell.text()).toContain('—')
  })
})

// ---- Branch coverage: verifyChain catch path ----
describe('AdminAuditView — verifyChain catch path', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('catches a network error and shows broken chain', async () => {
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/admin/audit/verify') throw new Error('network down')
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="admin-audit-verify-chain"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Chain Integrity: ❌ Broken')
    expect(wrapper.text()).toContain('network down')
  })
})

// ---- Branch coverage: exportCsv pagination with empty data ----
describe('AdminAuditView — exportCsv empty data break', () => {
  let clickSpy: ReturnType<typeof vi.fn>
  const originalCreateElement = document.createElement.bind(document)

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    clickSpy = vi.fn()
    URL.createObjectURL = vi.fn().mockReturnValue('blob:mock-url')
    URL.revokeObjectURL = vi.fn()
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
      if (tag === 'a') return { href: '', download: '', click: clickSpy } as unknown as HTMLAnchorElement
      return originalCreateElement(tag)
    }) as unknown as typeof document.createElement)
  })

  afterEach(() => { vi.restoreAllMocks() })

  it('exportCsv returns no data and stops pagination', async () => {
    // When the first export page returns undefined data, the loop breaks early.
    // The CSV is created with only headers (no data rows) and the download still fires.
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/admin/audit/export') return { data: undefined, error: undefined }
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="admin-audit-export-csv"]').trigger('click')
    await nextTick()
    await flushPromises()
    // The download fires even with empty data — the loop breaks but the CSV blob is still created
    expect(clickSpy).toHaveBeenCalledTimes(1)
  })
})

// ---- Branch coverage: exportJsonl catch path ----
describe('AdminAuditView — exportJsonl catch path', () => {
  const originalCreateElement = document.createElement.bind(document)

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    URL.createObjectURL = vi.fn().mockReturnValue('blob:mock-url')
    URL.revokeObjectURL = vi.fn()
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
      if (tag === 'a') return { href: '', download: '', click: vi.fn() } as unknown as HTMLAnchorElement
      return originalCreateElement(tag)
    }) as unknown as typeof document.createElement)
  })

  afterEach(() => { vi.restoreAllMocks() })

  it('catches a network error on JSONL export', async () => {
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/admin/audit/export') throw new Error('network down')
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="admin-audit-export-jsonl"]').trigger('click')
    await nextTick()
    await flushPromises()
    expect(wrapper.text()).toContain('Export failed')
    expect(wrapper.text()).toContain('network down')
  })
})

// ---- Branch coverage: buildQuery various filter combinations ----
describe('AdminAuditView — buildQuery filter branches', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('includes entity_type in query when filterTargetType is not __all__', async () => {
    mockAuditGet([auditEvent('evt-1')])
    const wrapper = await mountLoaded()
    const vm = wrapper.vm as unknown as { filterTargetType: string }
    vm.filterTargetType = 'pipeline'
    await nextTick()
    const calls = (api.GET as Mock).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/admin/audit')
    const q = calls[calls.length - 1][1].params.query
    expect(q.entity_type).toBe('pipeline')
  })

  it('excludes entity_type when filterTargetType is __all__', async () => {
    mockAuditGet([auditEvent('evt-1')])
    await mountLoaded()
    const calls = (api.GET as Mock).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/admin/audit')
    const q = calls[calls.length - 1][1].params.query
    expect(q.entity_type).toBeUndefined()
  })
})

// ---- Branch coverage: goToPage with null cursor ----
describe('AdminAuditView — goToPage null guard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('goToPage with null cursor is a no-op', async () => {
    mockAuditGet([auditEvent('evt-1')])
    const wrapper = await mountLoaded()
    const vm = wrapper.vm as unknown as { goToPage: (c: string | null) => void }
    vm.goToPage(null)
    await nextTick()
    // No crash, page unchanged
    expect(wrapper.text()).toContain('Page 1')
  })
})

// ---- Branch coverage: exportCsv with catch error ----
describe('AdminAuditView — exportCsv catch path', () => {
  let clickSpy: ReturnType<typeof vi.fn>
  const originalCreateElement = document.createElement.bind(document)

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    clickSpy = vi.fn()
    URL.createObjectURL = vi.fn().mockReturnValue('blob:mock-url')
    URL.revokeObjectURL = vi.fn()
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) => {
      if (tag === 'a') return { href: '', download: '', click: clickSpy } as unknown as HTMLAnchorElement
      return originalCreateElement(tag)
    }) as unknown as typeof document.createElement)
  })

  afterEach(() => { vi.restoreAllMocks() })

  it('catches a network error on CSV export', async () => {
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/admin/audit/export') throw new Error('csv down')
      return pagePayload([auditEvent('evt-1')])
    })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="admin-audit-export-csv"]').trigger('click')
    await nextTick()
    await flushPromises()
    expect(wrapper.text()).toContain('Export failed')
    expect(wrapper.text()).toContain('csv down')
  })
})
