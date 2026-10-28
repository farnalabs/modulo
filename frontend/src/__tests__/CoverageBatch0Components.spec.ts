import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import DataTable, { type Column } from '../components/ui/data-table/DataTable.vue'
import LogoMark from '../components/LogoMark.vue'
import ProvenanceBadge from '../components/lifecycle-map/ProvenanceBadge.vue'
import JourneyCard from '../components/lifecycle-map/JourneyCard.vue'
import type { JourneySummary } from '../types/lifecycleMap'

const teamMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
  operator: { value: true } as { value: boolean },
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: teamMocks.get,
    POST: teamMocks.post,
    PUT: teamMocks.put,
    DELETE: teamMocks.del,
  },
}))

vi.mock('../composables/useCurrentUser', () => ({
  useCurrentUser: () => ({ isOperator: teamMocks.operator }),
}))

import TeamNotificationEndpoints from '../components/TeamNotificationEndpoints.vue'

const teamEndpoints = [
  {
    id: 'ep-1',
    url: 'https://hooks.example.com/one',
    events: ['run_failed'],
    description: 'Primary',
    auto_disabled: false,
    consecutive_dead_letter_count: 0,
    team_id: 'team-a',
  },
  {
    id: 'ep-2',
    url: 'https://hooks.example.com/two',
    events: ['run_stalled'],
    description: null,
    auto_disabled: true,
    consecutive_dead_letter_count: 2,
    team_id: 'team-a',
  },
]

function availableEventsResponse() {
  return {
    data: ['run_failed', 'run_stalled', 'new_server_event'],
    error: undefined,
  }
}

function configureTeamApi() {
  teamMocks.get.mockImplementation((url: string) => {
    if (url === '/api/v1/notifications') {
      return Promise.resolve({ data: teamEndpoints, error: undefined })
    }
    return Promise.resolve(availableEventsResponse())
  })
  teamMocks.post.mockResolvedValue({
    data: { id: 'ep-3' },
    error: undefined,
  })
  teamMocks.put.mockResolvedValue({ data: null, error: undefined })
  teamMocks.del.mockResolvedValue({ response: { status: 204, ok: true }, error: undefined })
}

async function flush() {
  await new Promise<void>((resolve) => setTimeout(resolve, 0))
  await nextTick()
}

function mountTeam() {
  return mount(TeamNotificationEndpoints, {
    props: { teamId: 'team-a' },
    global: { stubs: { Tooltip: { template: '<span><slot /></span>' } } },
  })
}

describe('LogoMark', () => {
  it('uses the default size and renders its opaque background', () => {
    const wrapper = mount(LogoMark)
    const svg = wrapper.get('svg')
    expect(svg.attributes('width')).toBe('40')
    expect(svg.attributes('height')).toBe('40')
    expect(svg.find('rect').exists()).toBe(true)
    expect(svg.attributes('role')).toBe('img')
  })

  it('supports a custom size and transparent rendering', () => {
    const wrapper = mount(LogoMark, { props: { size: 72, transparent: true } })
    const svg = wrapper.get('svg')
    expect(svg.attributes('width')).toBe('72')
    expect(svg.attributes('height')).toBe('72')
    expect(svg.find('rect').exists()).toBe(false)
    expect(svg.findAll('polygon')).toHaveLength(2)
  })
})

describe('DataTable', () => {
  const columns: Column[] = [
    { key: 'name', label: 'Name', sortable: true, testid: 'sort-name' },
    { key: 'score', label: 'Score', sortable: true, numeric: true },
    { key: 'note', label: 'Note' },
  ]

  it('renders loading skeletons and respects the loadingRows prop', () => {
    const wrapper = mount(DataTable, { props: { columns, rows: [], loading: true, loadingRows: 2 } })
    expect(wrapper.findAll('tbody tr')).toHaveLength(2)
    expect(wrapper.findAll('.animate-pulse')).toHaveLength(6)
  })

  it('renders the empty slot when there are no rows', () => {
    const wrapper = mount(DataTable, {
      props: { columns, rows: [] },
      slots: { empty: '<strong data-testid="empty">Nothing here</strong>' },
    })
    expect(wrapper.get('[data-testid="empty"]').text()).toBe('Nothing here')
  })

  it('sorts numeric and nullable values and supports custom cell slots', async () => {
    const rows = [
      { id: 'a', name: 'zulu', score: null, note: 'first' },
      { id: 'b', name: 'alpha', score: 10, note: 'second' },
      { id: 'c', name: 'bravo', score: 2, note: 'third' },
    ]
    const wrapper = mount(DataTable, {
      props: { columns, rows, initialSortColumn: 'score', initialSortDirection: 'asc', rowTestid: 'data-row' },
      slots: { 'cell-note': ({ value }: { value: unknown }) => `Note: ${String(value)}` },
    })
    expect(wrapper.findAll('[data-testid="data-row"]')[0].text()).toContain('bravo')
    expect(wrapper.findAll('[data-testid="data-row"]')[2].text()).toContain('zulu')
    expect(wrapper.findAll('tbody td')[2].text()).toContain('Note: third')

    await wrapper.get('[data-testid="sort-name"]').trigger('click')
    expect(wrapper.find('th[aria-sort="ascending"]').text()).toContain('Name')
    expect(wrapper.findAll('[data-testid="data-row"]')[0].text()).toContain('alpha')
    await wrapper.get('[data-testid="sort-name"]').trigger('click')
    expect(wrapper.find('th[aria-sort="descending"]').exists()).toBe(true)
  })

  it('emits rows from mouse and keyboard only when clickable', async () => {
    const row = { id: 'row-1', name: 'alpha', score: 1, note: 'n' }
    const wrapper = mount(DataTable, { props: { columns, rows: [row] } })
    const tr = wrapper.get('tbody tr')
    await tr.trigger('click')
    await tr.trigger('keydown', { key: 'Enter' })
    await tr.trigger('keydown', { key: ' ' })
    await tr.trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('row-click')).toEqual([[row], [row], [row]])

    const inert = mount(DataTable, { props: { columns, rows: [row], rowClickable: false } })
    await inert.get('tbody tr').trigger('click')
    await inert.get('tbody tr').trigger('keydown', { key: 'Enter' })
    expect(inert.emitted('row-click')).toBeUndefined()
  })
})

describe('lifecycle journey components', () => {
  function journey(overrides: Partial<JourneySummary> = {}): JourneySummary {
    return {
      kind: 'linear',
      ref: 'FAR-1',
      canonical_work_item_id: '',
      current_stage: null,
      status: '',
      provenance: 'future',
      run_count: 0,
      unattributed: false,
      latest_run_id: null,
      updated_at: '',
      ...overrides,
    }
  }

  it('renders the fallback provenance label and slate class', () => {
    const wrapper = mount(ProvenanceBadge, { props: { provenance: 'self_reported' } })
    expect(wrapper.text()).toBe('self_reported')
    expect(wrapper.classes()).toContain('badge-context-slate')
    expect(wrapper.attributes('data-testid')).toBe('provenance-badge-self_reported')
  })

  it('renders no label for an absent provenance value', () => {
    const wrapper = mount(ProvenanceBadge, { props: { provenance: null } })
    expect(wrapper.text()).toBe('')
    expect(wrapper.find('span').exists()).toBe(false)
  })

  it('renders every journey status class and falls back to raw status text', () => {
    const statuses = [
      ['failed', 'badge-context-rose'],
      ['stalled', 'badge-context-rose'],
      ['eval_failed', 'badge-context-rose'],
      ['awaiting_human', 'badge-context-amber'],
      ['claimed', 'badge-context-amber'],
      ['complete', 'badge-context-green'],
      ['running', 'badge-context-blue'],
      ['new_status', 'badge-context-slate'],
    ] as const
    for (const [status, className] of statuses) {
      const wrapper = mount(JourneyCard, { props: { journey: journey({ status }) } })
      expect(wrapper.find(`[data-testid="journey-status-${status}"]`).classes()).toContain(className)
      expect(wrapper.text()).toContain(status === 'new_status' ? status : wrapper.find(`[data-testid="journey-status-${status}"]`).text())
    }
  })

  it('does not render a zero run count and emits open for the fallback journey', async () => {
    const wrapper = mount(JourneyCard, { props: { journey: journey({ provenance: null }) } })
    expect(wrapper.text()).not.toContain('×0')
    expect(wrapper.find('[data-testid="provenance-badge-null"]').exists()).toBe(false)
    await wrapper.trigger('click')
    expect(wrapper.emitted('open')).toHaveLength(1)
  })
})

describe('TeamNotificationEndpoints uncovered actions', () => {
  beforeEach(() => {
    configureTeamApi()
    teamMocks.operator.value = true
  })

  it('handles endpoint loading errors and retries', async () => {
    teamMocks.get.mockRejectedValueOnce(new Error('network down')).mockImplementation((url: string) => {
      if (url === '/api/v1/notifications') return Promise.resolve({ data: teamEndpoints, error: undefined })
      return Promise.resolve(availableEventsResponse())
    })
    const wrapper = mountTeam()
    await flush()
    expect(wrapper.text()).toContain('Failed to load endpoints')
    await wrapper.get('[data-testid="team-notif-retry"]').trigger('click')
    await flush()
    expect(wrapper.text()).toContain(teamEndpoints[0].url)
  })

  it('saves an edited endpoint and sends the optional secret and nullable description', async () => {
    const wrapper = mountTeam()
    await flush()
    await wrapper.find('[data-testid="team-notif-edit"]').trigger('click')
    await flush()
    await wrapper.get('[data-testid="team-notif-edit-url"]').setValue(' https://hooks.example.com/updated ')
    await wrapper.get('[data-testid="team-notif-edit-secret"]').setValue('secret')
    await wrapper.get('[data-testid="team-notif-edit-description"]').setValue('Updated')
    await wrapper.get('[data-testid="team-notif-edit-save"]').trigger('click')
    await flush()
    expect(teamMocks.put).toHaveBeenCalledWith('/api/v1/notifications/{endpoint_id}', expect.objectContaining({
      params: { path: { endpoint_id: 'ep-1' } },
      body: expect.objectContaining({ url: 'https://hooks.example.com/updated', secret: 'secret', description: 'Updated' }),
    }))
  })

  it('shows save errors and allows editing cancellation', async () => {
    teamMocks.put.mockResolvedValueOnce({ data: null, error: { detail: 'invalid endpoint' } })
    const wrapper = mountTeam()
    await flush()
    await wrapper.find('[data-testid="team-notif-edit"]').trigger('click')
    await wrapper.get('[data-testid="team-notif-edit-save"]').trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Save failed')
    await wrapper.get('[data-testid="team-notif-edit-cancel"]').trigger('click')
    expect(wrapper.find('[data-testid="team-notif-edit-url"]').exists()).toBe(false)
  })

  it('shows failed test responses and handles thrown test errors', async () => {
    teamMocks.post.mockResolvedValueOnce({ data: { success: false, status_code: 500, response_body: null, error: null }, error: undefined })
    const wrapper = mountTeam()
    await flush()
    await wrapper.find('[data-testid="team-notif-test"]').trigger('click')
    await flush()
    expect(wrapper.text()).toContain('HTTP 500')

    teamMocks.post.mockRejectedValueOnce(new Error('test unavailable'))
    await wrapper.findAll('[data-testid="team-notif-test"]')[1].trigger('click')
    await flush()
    expect(wrapper.text()).toContain('test unavailable')
  })

  it('surfaces delete errors and supports cancelling confirmation', async () => {
    teamMocks.del.mockResolvedValueOnce({ response: { status: 500, ok: false }, error: { detail: 'delete denied' } })
    const wrapper = mountTeam()
    await flush()
    await wrapper.find('[data-testid="team-notif-delete"]').trigger('click')
    await wrapper.get('[data-testid="team-notif-delete-confirm"]').trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Delete failed')

    const cancelWrapper = mountTeam()
    await flush()
    await cancelWrapper.find('[data-testid="team-notif-delete"]').trigger('click')
    const confirmation = cancelWrapper.get('[data-testid="team-notif-delete-confirm"]').element.parentElement
    expect(confirmation).not.toBeNull()
    const cancelButton = Array.from(confirmation!.querySelectorAll('button')).find(
      (button) => button !== cancelWrapper.get('[data-testid="team-notif-delete-confirm"]').element,
    )
    expect(cancelButton).toBeDefined()
    await cancelWrapper.findAll('button').find((button) => button.element === cancelButton)!.trigger('click')
    expect(cancelWrapper.find('[data-testid="team-notif-delete-confirm"]').exists()).toBe(false)
  })
})
