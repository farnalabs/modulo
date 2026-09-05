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
// useDataFetch, FAR-608). mountLoaded still applies the filters once — the
// real user path — before asserting, so the refetched state is what's checked.
async function mountLoaded() {
  const wrapper = mountView()
  await nextTick()
  await wrapper.find('[data-testid="admin-audit-apply-filters"]').trigger('click')
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
    await wrapper.find('[data-testid="admin-audit-apply-filters"]').trigger('click')
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

  it('apply filters resets the cursor and sends the actor, from-date and entity type', async () => {
    mockAuditGet([auditEvent('evt-1')], { total: 3 })
    const wrapper = await mountLoaded()

    await wrapper.find('[data-testid="admin-audit-actor"]').setValue('user-12345678')
    await wrapper.find('[data-testid="admin-audit-date-from"]').setValue('2026-08-01')
    await wrapper.find('[data-testid="admin-audit-date-to"]').setValue('2026-08-31')
    // The target-type filter is a PrimeVue Select — drive its ref directly.
    const vm = wrapper.vm as unknown as { filterTargetType: string }
    vm.filterTargetType = 'pipeline'
    await nextTick()

    await wrapper.find('[data-testid="admin-audit-apply-filters"]').trigger('click')
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
})
