import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

function adminJwt(): string {
  const header = btoa(JSON.stringify({ alg: 'HS256', typ: 'JWT' }))
  const payload = btoa(
    JSON.stringify({
      sub: 'test@example.com',
      org_id: '00000000-0000-0000-0000-000000000001',
      org_role: 'admin',
    }),
  )
  return `${header}.${payload}.signature`
}

const mockEndpoints = [
  {
    id: 'ep-1',
    url: 'https://hooks.example.com/webhook',
    events: ['run_failed', 'hitl_awaiting'],
    description: 'Production alerts',
    auto_disabled: false,
    consecutive_dead_letter_count: 0,
    team_id: 'team-alpha',
  },
  {
    id: 'ep-2',
    url: 'https://alerts.example.com/hitl',
    events: ['hitl_overdue'],
    description: null,
    auto_disabled: true,
    consecutive_dead_letter_count: 5,
    team_id: 'team-alpha',
  },
  {
    id: 'ep-other',
    url: 'https://other-team.example.com/hook',
    events: ['claim_expired'],
    description: null,
    auto_disabled: false,
    consecutive_dead_letter_count: 0,
    team_id: 'team-beta',
  },
]

const mockAvailableEvents = [
  'hitl_awaiting',
  'run_failed',
  'run_stalled',
  'claim_expired',
  'hitl_overdue',
  'hitl_deadline_warning',
  'budget_exceeded',
  'circuit_breaker_tripped',
  'trigger_deactivated',
]

// Hoisted function declaration so the (hoisted) vi.mock factory and the
// per-test beforeEach restore below both reference the SAME default GET
// behaviour — a test that overrides it never leaks its override.
function defaultGet(url: string): Promise<{ data: unknown; error: unknown }> {
  if (url === '/api/v1/notifications') {
    return Promise.resolve({ data: mockEndpoints, error: undefined })
  }
  if (url === '/api/v1/admin/notifications/available-events') {
    return Promise.resolve({ data: mockAvailableEvents, error: undefined })
  }
  return Promise.resolve({ data: null, error: undefined })
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => defaultGet(url)),
    POST: vi.fn().mockResolvedValue({ data: { id: 'ep-new', url: 'https://example.com/new', events: [], description: null, auto_disabled: false, consecutive_dead_letter_count: 0, team_id: 'team-alpha' }, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ response: { status: 204, ok: true }, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue(adminJwt()),
}))

import TeamNotificationEndpoints from '../components/TeamNotificationEndpoints.vue'
import { api } from '../lib/api/client'

async function flush() {
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
}

describe('TeamNotificationEndpoints', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation(
      (url: string) => defaultGet(url),
    )
  })

  const stubs = {
    Tooltip: { template: '<div><slot /></div>' },
    TooltipTrigger: { template: '<div><slot /></div>' },
    TooltipContent: { template: '<div><slot /></div>' },
  }

  it('renders without crashing and shows team endpoints', async () => {
    const wrapper = mount(TeamNotificationEndpoints, {
      props: { teamId: 'team-alpha' },
      global: { stubs },
    })
    await flush()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('https://hooks.example.com/webhook')
    expect(wrapper.text()).toContain('https://alerts.example.com/hitl')
  })

  it('filters endpoints for the given team', async () => {
    const wrapper = mount(TeamNotificationEndpoints, {
      props: { teamId: 'team-alpha' },
      global: { stubs },
    })
    await flush()
    expect(wrapper.text()).toContain('run_failed')
    expect(wrapper.text()).toContain('hitl_overdue')
    expect(wrapper.text()).not.toContain('https://other-team.example.com/hook')
  })

  it('shows empty state when no endpoints for team', async () => {
    const wrapper = mount(TeamNotificationEndpoints, {
      props: { teamId: 'team-empty' },
      global: { stubs },
    })
    await flush()
    expect(wrapper.text()).toContain('No webhook endpoints configured')
  })

  it('shows add form when Add webhook is clicked', async () => {
    const wrapper = mount(TeamNotificationEndpoints, {
      props: { teamId: 'team-alpha' },
      global: { stubs },
    })
    await flush()
    const addBtn = wrapper.find('[data-testid="team-notif-add-button"]')
    expect(addBtn.exists()).toBe(true)
    await addBtn.trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="team-notif-add-url"]').exists()).toBe(true)
  })

  it('creates a new endpoint', async () => {
    const wrapper = mount(TeamNotificationEndpoints, {
      props: { teamId: 'team-alpha' },
      global: { stubs },
    })
    await flush()
    await wrapper.find('[data-testid="team-notif-add-button"]').trigger('click')
    await nextTick()
    const urlInput = wrapper.find('[data-testid="team-notif-add-url"]')
    await urlInput.setValue('https://example.com/new-hook')
    await wrapper.find('[data-testid="team-notif-add-save"]').trigger('click')
    await flush()
    const { api } = await import('../lib/api/client')
    expect(api.POST).toHaveBeenCalledWith('/api/v1/notifications', expect.objectContaining({
      body: expect.objectContaining({
        url: 'https://example.com/new-hook',
        team_id: 'team-alpha',
      }),
    }))
  })

  it('offers hitl_deadline_warning in the picker and round-trips the subscription', async () => {
    const wrapper = mount(TeamNotificationEndpoints, {
      props: { teamId: 'team-alpha' },
      global: { stubs },
    })
    await flush()
    await wrapper.find('[data-testid="team-notif-add-button"]').trigger('click')
    await nextTick()

    // The FAR-1295 approaching-deadline event must be subscribable.
    const deadlineBox = wrapper
      .findAll('label')
      .find((label) => label.text().trim() === 'hitl_deadline_warning')
      ?.find('input[type="checkbox"]')
    expect(deadlineBox).toBeDefined()
    await deadlineBox?.setValue(true)

    await wrapper.find('[data-testid="team-notif-add-url"]').setValue('https://example.com/deadline-hook')
    await wrapper.find('[data-testid="team-notif-add-save"]').trigger('click')
    await flush()

    const { api } = await import('../lib/api/client')
    expect(api.POST).toHaveBeenCalledWith(
      '/api/v1/notifications',
      expect.objectContaining({
        body: expect.objectContaining({
          url: 'https://example.com/deadline-hook',
          events: expect.arrayContaining(['hitl_deadline_warning']),
        }),
      }),
    )
  })

  it('drives the picker from GET /api/v1/admin/notifications/available-events (FAR-1319)', async () => {
    const { api } = await import('../lib/api/client')
    const wrapper = mount(TeamNotificationEndpoints, {
      props: { teamId: 'team-alpha' },
      global: { stubs },
    })
    await flush()

    expect(api.GET).toHaveBeenCalledWith('/api/v1/admin/notifications/available-events')

    await wrapper.find('[data-testid="team-notif-add-button"]').trigger('click')
    await nextTick()

    const labels = wrapper.findAll('label').map((label) => label.text().trim())
    // Registry events the old hardcoded array omitted must be offered.
    expect(labels).toContain('run_stalled')
    expect(labels).toContain('circuit_breaker_tripped')
    expect(labels).toContain('trigger_deactivated')
  })

  it('shows a retryable message when the event registry fetch fails', async () => {
    vi.mocked(api.GET as unknown as (url: string) => Promise<unknown>).mockImplementation(
      (url: string) => {
        if (url === '/api/v1/notifications') {
          return Promise.resolve({ data: mockEndpoints, error: undefined })
        }
        return Promise.resolve({ data: undefined, error: { detail: 'boom' } })
      },
    )
    const wrapper = mount(TeamNotificationEndpoints, {
      props: { teamId: 'team-alpha' },
      global: { stubs },
    })
    await flush()
    await wrapper.find('[data-testid="team-notif-add-button"]').trigger('click')
    await nextTick()

    const msg = wrapper.find('[data-testid="team-notif-events-unavailable"]')
    expect(msg.exists()).toBe(true)
    expect(wrapper.find('[data-testid="team-notif-events-retry"]').exists()).toBe(true)
  })

  it('deletes an endpoint with confirmation', async () => {
    const wrapper = mount(TeamNotificationEndpoints, {
      props: { teamId: 'team-alpha' },
      global: { stubs },
    })
    await flush()
    const deleteBtns = wrapper.findAll('[data-testid="team-notif-delete"]')
    expect(deleteBtns.length).toBeGreaterThan(0)
    await deleteBtns[0].trigger('click')
    await nextTick()
    const confirmBtn = wrapper.find('[data-testid="team-notif-delete-confirm"]')
    expect(confirmBtn.exists()).toBe(true)
    await confirmBtn.trigger('click')
    await flush()
    const { api } = await import('../lib/api/client')
    expect(api.DELETE).toHaveBeenCalled()
  })

  it('shows test result after testing an endpoint', async () => {
    const { api } = await import('../lib/api/client')
    vi.mocked(api.POST as unknown as () => Promise<unknown>).mockResolvedValue({
      data: { success: true, status_code: 200, response_body: 'OK', error: null },
      error: undefined,
    })
    const wrapper = mount(TeamNotificationEndpoints, {
      props: { teamId: 'team-alpha' },
      global: { stubs },
    })
    await flush()
    const testBtns = wrapper.findAll('[data-testid="team-notif-test"]')
    expect(testBtns.length).toBeGreaterThan(0)
    await testBtns[0].trigger('click')
    await flush()
    expect(wrapper.text()).toContain('Test sent successfully')
  })
})
