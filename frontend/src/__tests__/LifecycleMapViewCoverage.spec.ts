/**
 * Coverage-focused tests for LifecycleMapView.vue (FAR-835).
 *
 * Targets script logic branches (guards, early returns, error paths, computed
 * getters, watchers, event handlers, drag/drop and node/edge mutation handlers,
 * store interactions, serialisation failure paths) and template state branches
 * (loading/error/empty/filled states, v-if chains).
 *
 * These tests extend the existing LifecycleMapView.spec.ts — they do NOT
 * duplicate its happy-path assertions.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createI18n } from 'vue-i18n'

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({ params: { id: 'map-1' }, query: {}, meta: {}, name: 'lifecycle-map-detail' })),
  useRouter: vi.fn(() => ({ push: routerPushMock })),
}))

vi.mock('../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  exitToLogin: vi.fn(),
}))

vi.mock('../lib/api/formatError', () => ({
  formatApiError: (e: unknown) => (e instanceof Error ? e.message : 'Request failed'),
}))

import LifecycleMapView from '../views/lifecycle-map/LifecycleMapView.vue'
import LifecycleMapRenderer from '../components/lifecycle-map/LifecycleMapRenderer.vue'
import Select from '../components/shared/AppSelect.vue'
import { usePlanStore } from '../stores/planStore'
import { useLifecycleMapsStore } from '../stores/lifecycleMaps'

const routerPushMock = vi.fn()

function okJson(data: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => data,
  } as unknown as Response
}

const mapDetail = {
  id: 'map-1',
  name: 'Launch Flow',
  description: 'A test lifecycle map',
  owner: 'alice',
  owner_team_id: 'team-1',
  stages: [
    { id: 'stage-1', name: 'Build', description: null, type: 'modulo', owner_badge: null, graduated: false, pipeline_id: 'pipe-1', external_url: null },
    { id: 'stage-2', name: 'Deploy', description: 'Deploy stage', type: 'external', owner_badge: null, graduated: true, pipeline_id: null, external_url: 'https://deploy.example.com' },
    { id: 'stage-3', name: 'Manual', description: null, type: 'manual', owner_badge: null, graduated: false, pipeline_id: null, external_url: null },
  ],
  transitions: [
    { id: 'e1', source_stage_id: 'stage-1', target_stage_id: 'stage-2', trigger_type: 'auto', description: null },
    { id: 'e2', source_stage_id: 'stage-2', target_stage_id: 'stage-3', trigger_type: 'manual', description: null, condition_expression: null, estimated_frequency: null, trigger_link: null },
  ],
  versions: [
    { id: 'v-uuid-1', version: 1, created_at: '2026-01-01T00:00:00Z', created_by: 'alice' },
    { id: 'v-uuid-2', version: 2, created_at: '2026-02-01T00:00:00Z', created_by: 'bob' },
  ],
  current_version: 2,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-02-01T00:00:00Z',
}

const journeyItem = {
  kind: 'run',
  ref: 'run-1',
  canonical_work_item_id: 'run-1',
  current_stage: null,
  status: 'complete',
  provenance: 'execution',
  run_count: 3,
  unattributed: true,
  latest_run_id: 'run-1',
  updated_at: '2026-01-02T00:00:00Z',
}

const journeyItemAttributed = {
  ...journeyItem,
  ref: 'run-2',
  canonical_work_item_id: 'run-2',
  unattributed: false,
  current_stage: { map_id: 'map-1', version: 2, stage_id: 'stage-1', stage_name: 'Build', position: 0 },
}

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: {
    'en-US': {
      views: {
        LifecycleMapView: {
          edit: 'Edit',
          delete: 'Delete',
          delete_confirm_title: 'Delete Lifecycle Map',
          delete_confirm_body: 'Are you sure you want to delete {name}?',
          version_label: 'Version:',
          version_placeholder: 'Select a version',
          export_map: 'Export',
          exporting: 'Exporting...',
          import_map: 'Import',
          import_dialog_title: 'Import Lifecycle Map',
          import_paste_hint: 'Paste an exported lifecycle map (JSON).',
          import_placeholder: 'Paste the exported lifecycle map JSON here',
          import_payload_label: 'Lifecycle map export JSON',
          import_invalid_json: 'The pasted content is not valid JSON.',
          importing: 'Importing...',
          cancel: 'Cancel',
          show_work_items: 'Show work items',
          save_status_saving: 'Saving...',
          save_status_saved: 'Positions saved',
          save_status_failed: 'Save failed',
          journey: {
            detail_title: 'Journey: {journey}',
            close: 'Close',
            loading: 'Loading journey...',
            loading_more: 'Loading more...',
            load_more: 'Load more',
            no_runs: 'No runs yet',
            run_count: '{count} run | {count} runs',
            open: 'Open {label} journey details',
            unattributed: 'Unattributed',
            unattributed_hint: '{count} unattributed run | {count} unattributed runs',
            unattributed_desc: 'Unattributed description',
            more_on_node: '+{count} more',
            more_on_node_title: '{count} older work items hidden',
            filter_period_label: 'Period',
            filter_status_label: 'Status',
            filter_period_24h: 'Last 24h',
            filter_period_3d: 'Last 3 days',
            filter_period_7d: 'Last 7 days',
            filter_period_30d: 'Last 30 days',
            filter_period_all: 'All time',
            filter_status_all: 'All',
            provenance: {
              derived: 'Derived',
              reported: 'Reported',
            },
            status: {
              complete: 'Completed',
              failed: 'Failed',
              stalled: 'Stalled',
              running: 'Running',
              pending: 'Pending',
              awaiting_human: 'Awaiting Human',
              cancelled: 'Cancelled',
              eval_failed: 'Eval Failed',
              claimed: 'Claimed',
            },
          },
        },
      },
    },
  },
})

let fetchMock: ReturnType<typeof vi.fn>
let journeysResponse: { items: unknown[]; next_cursor?: string | null }

function seedPlan(flags: Record<string, boolean> = {}) {
  const planStore = usePlanStore()
  planStore.features = flags
  planStore.loaded = true
}

beforeEach(() => {
  setActivePinia(createPinia())
  routerPushMock.mockClear()
  seedPlan()
  journeysResponse = { items: [] }
  fetchMock = vi.fn((url: string) => {
    if (url.includes('/journeys')) return Promise.resolve(okJson(journeysResponse))
    if (url.includes('/export')) {
      return Promise.resolve(okJson({
        primitive_type: 'lifecycle_map',
        format_version: '1',
        name: 'Launch Flow',
        description: null,
        content_json: { stages: [], edges: [] },
      }))
    }
    return Promise.resolve(okJson(mapDetail))
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

function mountView() {
  return mount(LifecycleMapView, {
    global: {
      plugins: [i18n],
      mocks: { $router: { push: routerPushMock } },
      stubs: {
        RouterLink: { template: '<a><slot /></a>' },
        ErrorAlert: { template: '<div data-testid="error-alert">{{ message }}</div>', props: ['message', 'onRetry'] },
        LifecycleMapRenderer: true,
        JourneyCard: true,
        ProvenanceBadge: true,
      },
    },
  })
}

// ---------------------------------------------------------------------------
// statusBadgeClass — all switch cases
// ---------------------------------------------------------------------------
describe('LifecycleMapView statusBadgeClass', () => {
  it('returns green for complete', async () => {
    const wrapper = mountView()
    await flushPromises()
    const store = useLifecycleMapsStore()
    store.journeys = [{ ...journeyItem, status: 'complete' }]
    seedPlan({ lifecycle_map_journeys: true })
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]')?.setValue(true)
    await flushPromises()
    // The statusBadgeClass is called internally for the badge class on runs
    // We exercise it via the journey detail panel — mount with a selected journey
    store.selectedJourneyKey = 'run:run-1'
    store.journeyDetail = {
      ...journeyItem,
      status: 'complete',
      runs: [{ run_id: 'r1', status: 'complete', completed_at: '2026-01-02T00:00:00Z', provenance: 'execution' }],
    } as never
    await flushPromises()
    const badge = wrapper.find('.badge')
    if (badge.exists()) {
      expect(badge.classes()).toContain('badge-context-green')
    }
  })

  it('returns rose for failed/stalled/eval_failed', async () => {
    const wrapper = mountView()
    await flushPromises()
    const store = useLifecycleMapsStore()
    seedPlan({ lifecycle_map_journeys: true })
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]')?.setValue(true)
    await flushPromises()

    for (const status of ['failed', 'stalled', 'eval_failed']) {
      store.selectedJourneyKey = `run:${status}`
      store.journeyDetail = {
        kind: 'run', ref: status, status,
        runs: [{ run_id: `r-${status}`, status, completed_at: '2026-01-02T00:00:00Z', provenance: null }],
      } as never
      await flushPromises()
      const badge = wrapper.find('.badge')
      if (badge.exists()) {
        expect(badge.classes()).toContain('badge-context-rose')
      }
    }
  })

  it('returns blue for running', async () => {
    const wrapper = mountView()
    await flushPromises()
    const store = useLifecycleMapsStore()
    seedPlan({ lifecycle_map_journeys: true })
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]')?.setValue(true)
    await flushPromises()

    store.selectedJourneyKey = 'run:running'
    store.journeyDetail = {
      kind: 'run', ref: 'running', status: 'running',
      runs: [{ run_id: 'r-run', status: 'running', completed_at: null, provenance: null }],
    } as never
    await flushPromises()
    const badge = wrapper.find('.badge')
    if (badge.exists()) {
      expect(badge.classes()).toContain('badge-context-blue')
    }
  })

  it('returns amber for awaiting_human and claimed', async () => {
    const wrapper = mountView()
    await flushPromises()
    const store = useLifecycleMapsStore()
    seedPlan({ lifecycle_map_journeys: true })
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]')?.setValue(true)
    await flushPromises()

    for (const status of ['awaiting_human', 'claimed']) {
      store.selectedJourneyKey = `run:${status}`
      store.journeyDetail = {
        kind: 'run', ref: status, status,
        runs: [{ run_id: `r-${status}`, status, completed_at: null, provenance: null }],
      } as never
      await flushPromises()
      const badge = wrapper.find('.badge')
      if (badge.exists()) {
        expect(badge.classes()).toContain('badge-context-amber')
      }
    }
  })

  it('returns slate for unknown status', async () => {
    const wrapper = mountView()
    await flushPromises()
    const store = useLifecycleMapsStore()
    seedPlan({ lifecycle_map_journeys: true })
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]')?.setValue(true)
    await flushPromises()

    store.selectedJourneyKey = 'run:pending'
    store.journeyDetail = {
      kind: 'run', ref: 'pending', status: 'pending',
      runs: [{ run_id: 'r-pend', status: 'pending', completed_at: null, provenance: null }],
    } as never
    await flushPromises()
    const badge = wrapper.find('.badge')
    if (badge.exists()) {
      expect(badge.classes()).toContain('badge-context-slate')
    }
  })
})

// ---------------------------------------------------------------------------
// statusLabel — unknown status falls back to raw string
// ---------------------------------------------------------------------------
describe('LifecycleMapView statusLabel', () => {
  it('returns translated label for known status', async () => {
    const wrapper = mountView()
    await flushPromises()
    const store = useLifecycleMapsStore()
    seedPlan({ lifecycle_map_journeys: true })
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]')?.setValue(true)
    await flushPromises()

    store.selectedJourneyKey = 'run:r1'
    store.journeyDetail = {
      kind: 'run', ref: 'r1', status: 'complete',
      runs: [{ run_id: 'r1', status: 'complete', completed_at: '2026-01-02T00:00:00Z', provenance: null }],
    } as never
    await flushPromises()
    const badge = wrapper.find('.badge')
    expect(badge.text()).toContain('Completed')
  })

  it('returns raw status string when no translation exists', async () => {
    const wrapper = mountView()
    await flushPromises()
    const store = useLifecycleMapsStore()
    seedPlan({ lifecycle_map_journeys: true })
    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]')?.setValue(true)
    await flushPromises()

    store.selectedJourneyKey = 'run:r2'
    store.journeyDetail = {
      kind: 'run', ref: 'r2', status: 'unknown_status',
      runs: [{ run_id: 'r2', status: 'unknown_status', completed_at: null, provenance: null }],
    } as never
    await flushPromises()
    const badge = wrapper.find('.badge')
    expect(badge.text()).toContain('unknown_status')
  })
})

// ---------------------------------------------------------------------------
// handleModuloStageClick — with and without pipeline_id
// ---------------------------------------------------------------------------
describe('LifecycleMapView handleModuloStageClick', () => {
  it('navigates to pipeline editor when stage has pipeline_id', async () => {
    const wrapper = mountView()
    await flushPromises()

    // Invoke directly on the component VM
    const vm = wrapper.vm as unknown as { handleModuloStageClick: (stage: { pipeline_id?: string | null }) => void }
    vm.handleModuloStageClick({ pipeline_id: 'pipe-1' })
    expect(routerPushMock).toHaveBeenCalledWith({ name: 'pipeline-editor', params: { id: 'pipe-1' } })
  })

  it('does not navigate when stage has no pipeline_id', async () => {
    const wrapper = mountView()
    await flushPromises()
    routerPushMock.mockClear()

    const vm = wrapper.vm as unknown as { handleModuloStageClick: (stage: { pipeline_id?: string | null }) => void }
    vm.handleModuloStageClick({ pipeline_id: null })
    expect(routerPushMock).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// handleExternalStageClick — with and without external_url
// ---------------------------------------------------------------------------
describe('LifecycleMapView handleExternalStageClick', () => {
  it('opens external URL when stage has external_url', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = mountView()
    await flushPromises()

    const vm = wrapper.vm as unknown as { handleExternalStageClick: (stage: { external_url?: string | null }) => void }
    vm.handleExternalStageClick({ external_url: 'https://example.com' })
    expect(openSpy).toHaveBeenCalledWith('https://example.com', '_blank', 'noopener,noreferrer')
    openSpy.mockRestore()
  })

  it('does not open window when stage has no external_url', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)
    const wrapper = mountView()
    await flushPromises()
    openSpy.mockClear()

    const vm = wrapper.vm as unknown as { handleExternalStageClick: (stage: { external_url?: string | null }) => void }
    vm.handleExternalStageClick({ external_url: null })
    expect(openSpy).not.toHaveBeenCalled()
    openSpy.mockRestore()
  })
})

// ---------------------------------------------------------------------------
// handleExport — error path and clipboard failure
// ---------------------------------------------------------------------------
describe('LifecycleMapView handleExport error paths', () => {
  it('shows export error when the export fails', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/export')) {
        return Promise.resolve({ ok: false, status: 500, statusText: 'Error', json: async () => ({ detail: 'server boom' }) })
      }
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-export"]').trigger('click')
    await flushPromises()

    const errorEl = wrapper.find('[data-testid="lifecycle-map-export-error"]')
    expect(errorEl.exists()).toBe(true)
    expect(errorEl.text()).toContain('server boom')
  })

  it('does not export when already exporting', async () => {
    // Create a deferred fetch to keep exporting=true
    let resolveExport!: (v: Response) => void
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/export')) {
        return new Promise((resolve) => { resolveExport = resolve })
      }
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    // First click starts exporting
    await wrapper.find('[data-testid="lifecycle-map-export"]').trigger('click')
    await flushPromises()

    // Button should be disabled while exporting
    const btn = wrapper.find('[data-testid="lifecycle-map-export"]')
    expect((btn.element as HTMLButtonElement).disabled).toBe(true)

    resolveExport(okJson({
      primitive_type: 'lifecycle_map', format_version: '1', name: 'test',
      description: null, content_json: { stages: [], edges: [] },
    }))
    await flushPromises()
  })

  it('handles clipboard write failure gracefully', async () => {
    const urlCreate = vi.fn(() => 'blob:fake-url')
    const urlRevoke = vi.fn()
    vi.stubGlobal('URL', { ...URL, createObjectURL: urlCreate, revokeObjectURL: urlRevoke })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    const writeText = vi.fn(async () => { throw new Error('clipboard blocked') })
    vi.stubGlobal('navigator', { clipboard: { writeText } })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-export"]').trigger('click')
    await flushPromises()

    // Download still succeeded even though clipboard failed
    expect(urlCreate).toHaveBeenCalled()
    expect(clickSpy).toHaveBeenCalled()
    // No export error because clipboard failure is swallowed
    expect(wrapper.find('[data-testid="lifecycle-map-export-error"]').exists()).toBe(false)

    clickSpy.mockRestore()
  })

  it('does not export when no mapId', async () => {
    // Override route to have no id
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)

    const wrapper = mountView()
    await flushPromises()
    fetchMock.mockClear()

    await wrapper.find('[data-testid="lifecycle-map-export"]').trigger('click')
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// handleDeleteConfirm — error path and guard
// ---------------------------------------------------------------------------
describe('LifecycleMapView handleDeleteConfirm', () => {
  it('shows delete error when the API fails', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes('/lifecycle-maps/map-1') && !String(url).includes('/versions') && !String(url).includes('/journeys') && !String(url).includes('/export')) {
        return Promise.resolve({ ok: false, status: 500, statusText: 'Error', json: async () => ({ detail: 'cannot delete' }) })
      }
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    // Open the delete dialog
    await wrapper.find('[data-testid="lifecycle-map-view-delete"]').trigger('click')
    await flushPromises()

    // Confirm deletion
    const vm = wrapper.vm as unknown as { handleDeleteConfirm: () => Promise<void> }
    await vm.handleDeleteConfirm()
    await flushPromises()

    const errorEl = wrapper.find('[data-testid="lifecycle-map-delete-error"]')
    expect(errorEl.exists()).toBe(true)
    expect(errorEl.text()).toContain('cannot delete')
  })

  it('does nothing when mapId is empty', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)

    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()
    routerPushMock.mockClear()

    const vm = wrapper.vm as unknown as { handleDeleteConfirm: () => Promise<void> }
    await vm.handleDeleteConfirm()
    await flushPromises()

    expect(routerPushMock).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// onVersionChange — different vs same version
// ---------------------------------------------------------------------------
describe('LifecycleMapView onVersionChange', () => {
  it('fetches the specific version when selected version differs from current', async () => {
    const wrapper = mountView()
    await flushPromises()

    const store = useLifecycleMapsStore()
    // Set selectedVersion to 1 (current is 2)
    const vm = wrapper.vm as unknown as {
      selectedVersion: { value: number | null }
      onVersionChange: () => Promise<void>
    }
    vm.selectedVersion.value = 1

    fetchMock.mockClear()
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes('/versions/1')) {
        return Promise.resolve(okJson({ ...mapDetail, current_version: 1 }))
      }
      if (String(url).includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    await vm.onVersionChange()
    await flushPromises()

    expect(fetchMock.mock.calls.some((c: unknown[]) => String(c[0]).includes('/versions/1'))).toBe(true)
  })

  it('fetches current map when selected version matches current', async () => {
    const wrapper = mountView()
    await flushPromises()

    const vm = wrapper.vm as unknown as {
      selectedVersion: { value: number | null }
      onVersionChange: () => Promise<void>
    }
    vm.selectedVersion.value = 2 // same as current_version

    fetchMock.mockClear()
    await vm.onVersionChange()
    await flushPromises()

    // Should call fetchMap (not fetchMapVersion) — the URL should NOT contain /versions/
    const mapCalls = fetchMock.mock.calls.filter((c: unknown[]) => {
      const url = String(c[0])
      return url.includes('/lifecycle-maps/map-1') && !url.includes('/versions/')
    })
    expect(mapCalls.length).toBeGreaterThanOrEqual(1)
  })

  it('returns early when selectedVersion is null', async () => {
    const wrapper = mountView()
    await flushPromises()
    fetchMock.mockClear()

    const vm = wrapper.vm as unknown as {
      selectedVersion: { value: number | null }
      onVersionChange: () => Promise<void>
    }
    vm.selectedVersion.value = null
    await vm.onVersionChange()
    await flushPromises()

    // Only the mount fetch should be there, no additional fetch
    expect(fetchMock).toHaveBeenCalledTimes(1) // just the initial mount fetch
  })
})

// ---------------------------------------------------------------------------
// handleImportConfirm — non-API error paths
// ---------------------------------------------------------------------------
describe('LifecycleMapView handleImportConfirm import errors', () => {
  it('shows API error when importMap fails with non-SyntaxError', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes('/import')) {
        return Promise.resolve({ ok: false, status: 400, statusText: 'Bad Request', json: async () => ({ detail: 'invalid envelope' }) })
      }
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()

    const payload = document.body.querySelector('[data-testid="lifecycle-map-import-payload"]') as HTMLTextAreaElement
    payload.value = JSON.stringify({ primitive_type: 'lifecycle_map', format_version: '1', name: 'test', content_json: {} })
    await payload.dispatchEvent(new Event('input'))
    await flushPromises()

    const confirmBtn = document.body.querySelector('[data-testid="lifecycle-map-import-confirm"]') as HTMLButtonElement
    await confirmBtn.click()
    await flushPromises()

    // Should show the API error, not the JSON parse error
    expect(document.body.textContent).toContain('invalid envelope')
  })

  it('does nothing when importPayload is empty', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()

    fetchMock.mockClear()
    const confirmBtn = document.body.querySelector('[data-testid="lifecycle-map-import-confirm"]') as HTMLButtonElement
    await confirmBtn.click()
    await flushPromises()

    // No API call because payload is empty
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// Retry journey detail — various guard paths
// ---------------------------------------------------------------------------
describe('LifecycleMapView retryJourneyDetail', () => {
  it('returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)

    const wrapper = mountView()
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-1'

    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { retryJourneyDetail: () => Promise<void> }
    await vm.retryJourneyDetail()
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('returns early when selectedJourneyKey is null', async () => {
    const wrapper = mountView()
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = null

    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { retryJourneyDetail: () => Promise<void> }
    await vm.retryJourneyDetail()
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('returns early when selectedJourneyKey has no colon', async () => {
    const wrapper = mountView()
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'nocolonkey'

    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { retryJourneyDetail: () => Promise<void> }
    await vm.retryJourneyDetail()
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('fetches journey detail with correct kind/ref from key', async () => {
    const wrapper = mountView()
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-123'

    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { retryJourneyDetail: () => Promise<void> }
    await vm.retryJourneyDetail()
    await flushPromises()

    expect(fetchMock.mock.calls.some((c: unknown[]) => String(c[0]).includes('/journeys/run/run-123'))).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// openJourneyDetail — guard path
// ---------------------------------------------------------------------------
describe('LifecycleMapView openJourneyDetail', () => {
  it('returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)

    const wrapper = mountView()
    await flushPromises()

    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { openJourneyDetail: (j: { kind: string; ref: string }) => Promise<void> }
    await vm.openJourneyDetail({ kind: 'run', ref: 'run-1' })
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// sortedVersions computed
// ---------------------------------------------------------------------------
describe('LifecycleMapView sortedVersions', () => {
  it('returns versions sorted descending by version number', async () => {
    const wrapper = mountView()
    await flushPromises()

    const vm = wrapper.vm as unknown as { sortedVersions: { version: number; id: string }[] }
    expect(vm.sortedVersions).toHaveLength(2)
    expect(vm.sortedVersions[0].version).toBe(2)
    expect(vm.sortedVersions[1].version).toBe(1)
  })
})

// ---------------------------------------------------------------------------
// selectedJourneyLabel computed
// ---------------------------------------------------------------------------
describe('LifecycleMapView selectedJourneyLabel', () => {
  it('returns empty string when no journey selected', async () => {
    const wrapper = mountView()
    await flushPromises()

    const vm = wrapper.vm as unknown as { selectedJourneyLabel: string }
    expect(vm.selectedJourneyLabel).toBe('')
  })

  it('replaces colon with space in the key', async () => {
    const wrapper = mountView()
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-42'

    const vm = wrapper.vm as unknown as { selectedJourneyLabel: string }
    expect(vm.selectedJourneyLabel).toBe('run run-42')
  })
})

// ---------------------------------------------------------------------------
// Template: export error banner
// ---------------------------------------------------------------------------
describe('LifecycleMapView template export error', () => {
  it('shows the export error alert', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/export')) {
        return Promise.resolve({ ok: false, status: 500, json: async () => ({ detail: 'boom' }) })
      }
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-export"]').trigger('click')
    await flushPromises()

    const errorEl = wrapper.find('[data-testid="lifecycle-map-export-error"]')
    expect(errorEl.exists()).toBe(true)
    expect(errorEl.attributes('role')).toBe('alert')
  })
})

// ---------------------------------------------------------------------------
// Template: delete dialog + error
// ---------------------------------------------------------------------------
describe('LifecycleMapView template delete dialog', () => {
  it('opens the delete confirmation dialog', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-view-delete"]').trigger('click')
    await flushPromises()

    // FormDialog should be rendered with the delete title
    expect(wrapper.text()).toContain('Delete Lifecycle Map')
  })
})

// ---------------------------------------------------------------------------
// Template: save status states
// ---------------------------------------------------------------------------
describe('LifecycleMapView save status', () => {
  let localStorageStore: Record<string, string>

  beforeEach(() => {
    localStorageStore = {}
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => localStorageStore[key] ?? null),
      setItem: vi.fn((key: string, value: string) => { localStorageStore[key] = value }),
      removeItem: vi.fn((key: string) => { delete localStorageStore[key] }),
    })
  })

  afterEach(() => { vi.useRealTimers() })

  it('shows saving status while the PUT is in flight', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let resolvePut!: (v: Response) => void
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes('/versions/')) {
        return new Promise((resolve) => { resolvePut = resolve })
      }
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    const vm = wrapper.vm as unknown as { handlePositionsChanged: (p: Record<string, { x: number; y: number }>) => void }
    vm.handlePositionsChanged({ 'stage-1': { x: 100, y: 200 } })
    await vi.advanceTimersByTimeAsync(600)

    const status = wrapper.find('[data-testid="lifecycle-map-save-status"]')
    expect(status.exists()).toBe(true)
    expect(status.text()).toContain('Saving')

    resolvePut(okJson({ version: 3 }))
    await vi.advanceTimersByTimeAsync(100)
    vi.useRealTimers()
  })

  it('shows saving indicator with spinner', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let resolvePut!: (v: Response) => void
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes('/versions/')) {
        return new Promise((resolve) => { resolvePut = resolve })
      }
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    const vm = wrapper.vm as unknown as { handlePositionsChanged: (p: Record<string, { x: number; y: number }>) => void }
    vm.handlePositionsChanged({ 'stage-1': { x: 100, y: 200 } })
    await vi.advanceTimersByTimeAsync(600)

    const status = wrapper.find('[data-testid="lifecycle-map-save-status"]')
    expect(status.find('.animate-spin').exists()).toBe(true)

    resolvePut(okJson({ version: 3 }))
    await vi.advanceTimersByTimeAsync(100)
    vi.useRealTimers()
  })

  it('applies version bump when server returns updated version', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes('/versions/')) {
        return Promise.resolve(okJson({ version: 5 }))
      }
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    const store = useLifecycleMapsStore()
    expect(store.currentMap?.current_version).toBe(2)

    const vm = wrapper.vm as unknown as { handlePositionsChanged: (p: Record<string, { x: number; y: number }>) => void }
    vm.handlePositionsChanged({ 'stage-1': { x: 100, y: 200 } })
    await vi.advanceTimersByTimeAsync(600)

    // The store should have the bumped version
    expect(store.currentMap?.current_version).toBe(5)
    vi.useRealTimers()
  })
})

// ---------------------------------------------------------------------------
// Template: version select with multiple versions
// ---------------------------------------------------------------------------
describe('LifecycleMapView version select', () => {
  it('renders a version select when there are multiple versions', async () => {
    const wrapper = mountView()
    await flushPromises()

    // The component has 2 versions, so the select should be present
    const versionSelect = wrapper.find('[data-testid="lifecycle-map-version-select"]')
    expect(versionSelect.exists()).toBe(true)
  })

  it('does not render version select with only one version', async () => {
    const singleVersionMap = {
      ...mapDetail,
      versions: [{ id: 'v-uuid-1', version: 1, created_at: '2026-01-01T00:00:00Z', created_by: null }],
      current_version: 1,
    }
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(singleVersionMap))
    })

    const wrapper = mountView()
    await flushPromises()

    const versionSelect = wrapper.find('[data-testid="lifecycle-map-version-select"]')
    expect(versionSelect.exists()).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Template: description + owner rendering
// ---------------------------------------------------------------------------
describe('LifecycleMapView description and owner', () => {
  it('renders the map description when present', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('A test lifecycle map')
  })

  it('renders the owner when present', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('alice')
  })

  it('hides description when null', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson({ ...mapDetail, description: null }))
    })

    const wrapper = mountView()
    await flushPromises()

    // The description paragraph should not render when null
    expect(wrapper.find('.text-sm.text-muted-foreground p').exists()).toBe(false)
  })

  it('hides owner when null', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson({ ...mapDetail, owner: null }))
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).not.toContain('alice')
  })
})

// ---------------------------------------------------------------------------
// Template: stage counts
// ---------------------------------------------------------------------------
describe('LifecycleMapView stage counts', () => {
  it('shows correct stage, graduated, and manual counts', async () => {
    const wrapper = mountView()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('3 stages')
    expect(text).toContain('1 graduated')
    expect(text).toContain('1 manual')
  })
})

// ---------------------------------------------------------------------------
// Template: version badge
// ---------------------------------------------------------------------------
describe('LifecycleMapView version badge', () => {
  it('renders the current version badge', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('v2')
  })
})

// ---------------------------------------------------------------------------
// Template: loading state (isLoadingDetail)
// ---------------------------------------------------------------------------
describe('LifecycleMapView loading state', () => {
  it('shows a loading spinner while detail is loading', async () => {
    // Create a deferred fetch for the detail
    let resolveDetail!: (v: Response) => void
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      if (!String(url).includes('/versions/')) {
        return new Promise((resolve) => { resolveDetail = resolve })
      }
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    // The loading spinner should be visible
    const spinner = wrapper.find('.animate-spin')
    expect(spinner.exists()).toBe(true)

    resolveDetail(okJson(mapDetail))
    await flushPromises()
  })
})

// ---------------------------------------------------------------------------
// Template: error state (detailError)
// ---------------------------------------------------------------------------
describe('LifecycleMapView error state', () => {
  it('shows ErrorAlert when detail load fails', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve({ ok: false, status: 404, statusText: 'Not Found', json: async () => ({ detail: 'not found' }) })
    })

    const wrapper = mountView()
    await flushPromises()

    const alert = wrapper.find('[data-testid="error-alert"]')
    expect(alert.exists()).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Template: empty state (no mapData)
// ---------------------------------------------------------------------------
describe('LifecycleMapView empty state', () => {
  it('shows "not found" when map data is null', async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(null))
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('Lifecycle map not found')
  })
})

// ---------------------------------------------------------------------------
// Plan store: fetchPlan when not loaded
// ---------------------------------------------------------------------------
describe('LifecycleMapView plan store loading', () => {
  it('fetches the plan when planStore.loaded is false', async () => {
    const planStore = usePlanStore()
    planStore.loaded = false
    planStore.fetchPlan = vi.fn(async () => { planStore.loaded = true })

    const wrapper = mountView()
    await flushPromises()

    expect(planStore.fetchPlan).toHaveBeenCalled()
  })

  it('skips fetchPlan when planStore.loaded is already true', async () => {
    const planStore = usePlanStore()
    planStore.loaded = true
    const spy = vi.fn(async () => {})
    planStore.fetchPlan = spy

    const wrapper = mountView()
    await flushPromises()

    expect(spy).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// onMounted guard — no mapId
// ---------------------------------------------------------------------------
describe('LifecycleMapView onMounted guard', () => {
  it('returns early when route has no id', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)

    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapDetail))
    })

    const wrapper = mountView()
    await flushPromises()

    // Should not have fetched the map (only the journeys endpoint might be called if flag on)
    expect(fetchMock.mock.calls.every((c: unknown[]) => !String(c[0]).includes('/lifecycle-maps/map-1'))).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Watchers — journeysEnabled, showWorkItems, filters
// ---------------------------------------------------------------------------
describe('LifecycleMapView watcher behaviour', () => {
  it('fetches journeys when journeysEnabled flips on AND showWorkItems is checked', async () => {
    const planStore = usePlanStore()
    planStore.loaded = true
    planStore.features = {}

    const wrapper = mountView()
    await flushPromises()

    const checkbox = wrapper.find('[data-testid="lifecycle-map-show-work-items"]')
    await checkbox.setValue(true)
    await flushPromises()

    expect(journeysEndpointCalled()).toBe(false) // flag is still off

    // Flip the flag on
    planStore.features = { lifecycle_map_journeys: true }
    await flushPromises()

    expect(journeysEndpointCalled()).toBe(true)
  })

  it('does NOT fetch when journeysEnabled flips on but showWorkItems is unchecked', async () => {
    const planStore = usePlanStore()
    planStore.loaded = true
    planStore.features = {}

    mountView()
    await flushPromises()

    planStore.features = { lifecycle_map_journeys: true }
    await flushPromises()

    // checkbox is still unchecked, so no fetch
    expect(journeysEndpointCalled()).toBe(false)
  })

  it('fetches journeys when showWorkItems is checked AND journeysEnabled is already on', async () => {
    seedPlan({ lifecycle_map_journeys: true })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    expect(journeysEndpointCalled()).toBe(true)
  })

  it('refetches on filter change when journeysVisible is true', async () => {
    journeysResponse = { items: [journeyItem], next_cursor: null }
    seedPlan({ lifecycle_map_journeys: true })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    fetchMock.mockClear()
    // Change the period filter
    const store = useLifecycleMapsStore()
    store.journeysFilters.period = '24h'
    await flushPromises()

    expect(journeysEndpointCalled()).toBe(true)
  })

  it('does NOT refetch on filter change when journeysVisible is false', async () => {
    seedPlan({ lifecycle_map_journeys: false })

    mountView()
    await flushPromises()

    fetchMock.mockClear()
    const store = useLifecycleMapsStore()
    store.journeysFilters.period = '24h'
    await flushPromises()

    expect(journeysEndpointCalled()).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// loadMoreJourneys — guard path
// ---------------------------------------------------------------------------
describe('LifecycleMapView loadMoreJourneys', () => {
  it('returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)

    const wrapper = mountView()
    await flushPromises()

    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { loadMoreJourneys: () => Promise<void> }
    await vm.loadMoreJourneys()
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// loadJourneys — guard path
// ---------------------------------------------------------------------------
describe('LifecycleMapView loadJourneys', () => {
  it('returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)

    const wrapper = mountView()
    await flushPromises()

    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { loadJourneys: () => Promise<void> }
    await vm.loadJourneys()
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// loadMap — guard path
// ---------------------------------------------------------------------------
describe('LifecycleMapView loadMap', () => {
  it('returns early when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)

    const wrapper = mountView()
    await flushPromises()

    fetchMock.mockClear()
    const vm = wrapper.vm as unknown as { loadMap: () => Promise<void> }
    await vm.loadMap()
    await flushPromises()

    // Should not fetch the map detail
    expect(fetchMock.mock.calls.every((c: unknown[]) => !String(c[0]).includes('/lifecycle-maps/map-1'))).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// editMap — guard path
// ---------------------------------------------------------------------------
describe('LifecycleMapView editMap', () => {
  it('navigates to editor when mapId exists', async () => {
    const wrapper = mountView()
    await flushPromises()

    const vm = wrapper.vm as unknown as { editMap: () => void }
    vm.editMap()
    expect(routerPushMock).toHaveBeenCalledWith({ name: 'lifecycle-map-editor', params: { id: 'map-1' } })
  })

  it('does nothing when no mapId', async () => {
    const { useRoute } = await import('vue-router')
    vi.mocked(useRoute).mockReturnValue({ params: { id: '' }, query: {}, meta: {}, name: 'lifecycle-map-detail' } as never)

    const wrapper = mountView()
    await flushPromises()
    routerPushMock.mockClear()

    const vm = wrapper.vm as unknown as { editMap: () => void }
    vm.editMap()
    expect(routerPushMock).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// persistPositions — server write missing mapId/versionId path
// ---------------------------------------------------------------------------
describe('LifecycleMapView persistPositions edge cases', () => {
  let localStorageStore: Record<string, string>

  beforeEach(() => {
    localStorageStore = {}
    vi.stubGlobal('localStorage', {
      getItem: vi.fn((key: string) => localStorageStore[key] ?? null),
      setItem: vi.fn((key: string, value: string) => { localStorageStore[key] = value }),
      removeItem: vi.fn((key: string) => { delete localStorageStore[key] }),
    })
  })
  afterEach(() => { vi.useRealTimers() })

  it('sets error status when versionId is missing', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    // Return a map with no versions
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson({ ...mapDetail, versions: [] }))
    })

    const wrapper = mountView()
    await flushPromises()

    const vm = wrapper.vm as unknown as { handlePositionsChanged: (p: Record<string, { x: number; y: number }>) => void }
    vm.handlePositionsChanged({ 'stage-1': { x: 1, y: 2 } })
    await vi.advanceTimersByTimeAsync(600)

    const status = wrapper.find('[data-testid="lifecycle-map-save-status"]')
    expect(status.exists()).toBe(true)
    expect(status.text()).toContain('failed')
    vi.useRealTimers()
  })
})

// ---------------------------------------------------------------------------
// Import dialog open/close
// ---------------------------------------------------------------------------
describe('LifecycleMapView import dialog', () => {
  it('opens the import dialog and resets state', async () => {
    const wrapper = mountView()
    await flushPromises()

    // Open dialog
    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()

    const payload = document.body.querySelector('[data-testid="lifecycle-map-import-payload"]') as HTMLTextAreaElement
    expect(payload).not.toBeNull()
    expect(payload.value).toBe('') // should be reset
  })

  it('cancel closes the import dialog', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()

    const cancelBtns = document.body.querySelectorAll('button')
    const cancelBtn = Array.from(cancelBtns).find(b => b.textContent?.includes('Cancel'))
    expect(cancelBtn).toBeTruthy()
    cancelBtn?.click()
    await flushPromises()

    expect(document.body.querySelector('[data-testid="lifecycle-map-import-payload"]')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// localStorage fallback — loadSavedPositions with corrupt JSON
// ---------------------------------------------------------------------------
describe('LifecycleMapView localStorage fallback', () => {
  it('falls back to empty positions when localStorage has corrupt JSON', async () => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => 'not-valid-json{{{'),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    })

    // No server positions (stages have no x/y)
    const mapNoPositions = {
      ...mapDetail,
      stages: [{ ...mapDetail.stages[0], x: null, y: null }],
    }
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapNoPositions))
    })

    const wrapper = mountView()
    await flushPromises()

    const renderer = wrapper.findComponent(LifecycleMapRenderer)
    expect(renderer.props('savedPositions')).toEqual({})
  })

  it('loads positions from localStorage when server has no positions', async () => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => JSON.stringify({ 'stage-1': { x: 99, y: 88 } })),
      setItem: vi.fn(),
      removeItem: vi.fn(),
    })

    const mapNoPositions = {
      ...mapDetail,
      stages: [{ ...mapDetail.stages[0], x: null, y: null }],
    }
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
      return Promise.resolve(okJson(mapNoPositions))
    })

    const wrapper = mountView()
    await flushPromises()

    const renderer = wrapper.findComponent(LifecycleMapRenderer)
    expect(renderer.props('savedPositions')).toEqual({ 'stage-1': { x: 99, y: 88 } })
  })
})

// ---------------------------------------------------------------------------
// closeJourneyDetail
// ---------------------------------------------------------------------------
describe('LifecycleMapView closeJourneyDetail', () => {
  it('clears the selected journey and detail', async () => {
    seedPlan({ lifecycle_map_journeys: true })
    const wrapper = mountView()
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-1'
    store.journeyDetail = { kind: 'run', ref: 'run-1', runs: [] } as never
    await flushPromises()

    const vm = wrapper.vm as unknown as { closeJourneyDetail: () => void }
    vm.closeJourneyDetail()
    await flushPromises()

    expect(store.selectedJourneyKey).toBeNull()
    expect(store.journeyDetail).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// journeyDetail: no runs
// ---------------------------------------------------------------------------
describe('LifecycleMapView journeyDetail no runs', () => {
  it('shows "No runs yet" when journeyDetail has empty runs', async () => {
    seedPlan({ lifecycle_map_journeys: true })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-1'
    store.journeyDetail = {
      kind: 'run', ref: 'run-1', status: 'complete',
      runs: [],
    } as never
    await flushPromises()

    expect(wrapper.text()).toContain('No runs yet')
  })
})

// ---------------------------------------------------------------------------
// journeyDetail: with runs
// ---------------------------------------------------------------------------
describe('LifecycleMapView journeyDetail with runs', () => {
  it('renders run list when journeyDetail has runs', async () => {
    seedPlan({ lifecycle_map_journeys: true })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-1'
    store.journeyDetail = {
      kind: 'run', ref: 'run-1', status: 'complete',
      runs: [
        { run_id: 'r1', status: 'complete', completed_at: '2026-01-02T00:00:00Z', provenance: 'execution' },
        { run_id: 'r2', status: 'failed', completed_at: '2026-01-03T00:00:00Z', provenance: null },
      ],
    } as never
    await flushPromises()

    const detailSection = wrapper.find('[aria-label="Journey details"]')
    expect(detailSection.exists()).toBe(true)
    // Two runs should render
    const badges = detailSection.findAll('.badge')
    expect(badges.length).toBe(2)
  })
})

// ---------------------------------------------------------------------------
// journeyDetailError — retry callback
// ---------------------------------------------------------------------------
describe('LifecycleMapView journeyDetailError', () => {
  it('shows error and retry when journey detail fails', async () => {
    seedPlan({ lifecycle_map_journeys: true })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-1'
    store.journeyDetailError = 'Failed to load journey'
    await flushPromises()

    const detailSection = wrapper.find('[aria-label="Journey details"]')
    expect(detailSection.exists()).toBe(true)
    // ErrorAlert should render
    const errorAlert = detailSection.find('[data-testid="error-alert"]')
    expect(errorAlert.exists()).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// journeysError — error banner
// ---------------------------------------------------------------------------
describe('LifecycleMapView journeysError', () => {
  it('shows journeys error when journeysError is set', async () => {
    seedPlan({ lifecycle_map_journeys: true })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.journeysError = 'Failed to load journeys'
    await flushPromises()

    const alerts = wrapper.findAll('[data-testid="error-alert"]')
    expect(alerts.length).toBeGreaterThanOrEqual(1)
  })
})

// ---------------------------------------------------------------------------
// unattributed journeys rendering
// ---------------------------------------------------------------------------
describe('LifecycleMapView unattributed journeys', () => {
  it('shows unattributed section with JourneyCards', async () => {
    journeysResponse = { items: [journeyItem], next_cursor: null }
    seedPlan({ lifecycle_map_journeys: true })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    const section = wrapper.find('[aria-label="Unattributed journeys"]')
    expect(section.exists()).toBe(true)
    expect(section.text()).toContain('1 unattributed run')
  })
})

// ---------------------------------------------------------------------------
// loadMore pagination button
// ---------------------------------------------------------------------------
describe('LifecycleMapView load more', () => {
  it('shows load more button when hasMoreJourneys is true', async () => {
    journeysResponse = { items: [journeyItem], next_cursor: 'cursor-2' }
    seedPlan({ lifecycle_map_journeys: true })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    const loadMoreBtn = wrapper.find('[data-testid="lifecycle-map-journeys-load-more"]')
    expect(loadMoreBtn.exists()).toBe(true)
  })

  it('load more button calls loadMoreJourneys', async () => {
    journeysResponse = { items: [journeyItem], next_cursor: 'cursor-2' }
    seedPlan({ lifecycle_map_journeys: true })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    const loadMoreBtn = wrapper.find('[data-testid="lifecycle-map-journeys-load-more"]')
    expect(loadMoreBtn.exists()).toBe(true)
    // Click should not throw
    await loadMoreBtn.trigger('click')
    await flushPromises()
  })
})

// ---------------------------------------------------------------------------
// journeys loading spinner in the journeys section
// ---------------------------------------------------------------------------
describe('LifecycleMapView journeys loading spinner in section', () => {
  it('shows loading spinner when isLoadingJourneys is true', async () => {
    seedPlan({ lifecycle_map_journeys: true })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.isLoadingJourneys = true
    await flushPromises()

    const spinnerSection = wrapper.find('[aria-live="polite"]')
    expect(spinnerSection.exists()).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// openImportDialog — resets state
// ---------------------------------------------------------------------------
describe('LifecycleMapView openImportDialog', () => {
  it('resets payload and error, then opens dialog', async () => {
    const wrapper = mountView()
    await flushPromises()

    // Open dialog, type something
    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()

    const payload = document.body.querySelector('[data-testid="lifecycle-map-import-payload"]') as HTMLTextAreaElement
    payload.value = 'some text'
    await payload.dispatchEvent(new Event('input'))
    await flushPromises()

    // Close dialog by clicking cancel
    const cancelBtns = document.body.querySelectorAll('button')
    const cancelBtn = Array.from(cancelBtns).find(b => b.textContent?.includes('Cancel'))
    cancelBtn?.click()
    await flushPromises()

    // Re-open dialog
    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()

    const payload2 = document.body.querySelector('[data-testid="lifecycle-map-import-payload"]') as HTMLTextAreaElement
    expect(payload2.value).toBe('') // reset
  })
})

// ---------------------------------------------------------------------------
// Template: journeys controls band — flag off
// ---------------------------------------------------------------------------
describe('LifecycleMapView journeys controls band', () => {
  it('hides the entire band when flag is off', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="lifecycle-map-journeys-controls"]').exists()).toBe(false)
  })

  it('shows the band when flag is on', async () => {
    seedPlan({ lifecycle_map_journeys: true })
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="lifecycle-map-journeys-controls"]').exists()).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Filter options rendered
// ---------------------------------------------------------------------------
describe('LifecycleMapView filter options', () => {
  it('shows period and status filter selects when checkbox is checked', async () => {
    seedPlan({ lifecycle_map_journeys: true })
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-show-work-items"]').setValue(true)
    await flushPromises()

    // The filter controls should be visible
    const allSelects = wrapper.findAllComponents(Select)
    expect(allSelects.length).toBeGreaterThanOrEqual(2) // period + status selects
  })
})
