import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

type ApiResult = { data?: unknown; error?: Record<string, unknown> | undefined; response?: { status: number; ok: boolean } }

let mockTeams: Array<Record<string, unknown>> = []
let mockUsers: Array<Record<string, unknown>> = []
let mockMembersByTeam: Record<string, Array<Record<string, unknown>>> = {}
let mockListError: Record<string, unknown> | undefined
let mockMembersError: Record<string, unknown> | undefined
let postResult: ApiResult = { data: null, error: undefined }
let putResult: ApiResult = { data: null, error: undefined }
let deleteResult: ApiResult = { response: { status: 204, ok: true }, error: undefined }

function res(data: unknown, error?: Record<string, unknown>): ApiResult {
  return { data, error: error as ApiResult['error'] }
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/admin/teams') {
        if (mockListError) return Promise.resolve(res(null, mockListError))
        return Promise.resolve(res({ items: mockTeams }))
      }
      if (url === '/api/v1/admin/users') return Promise.resolve(res({ items: mockUsers }))
      if (url.startsWith('/api/v1/teams/') && url.endsWith('/members')) {
        // The api client substitutes path params internally, so the mock sees
        // the templated route; serve the single-team fixture regardless.
        if (mockMembersError) return Promise.resolve(res(null, mockMembersError))
        return Promise.resolve(res({ items: mockMembersByTeam.t1 ?? [] }))
      }
      if (url === '/api/v1/notifications') return Promise.resolve(res([]))
      return Promise.resolve(res({ items: [] }))
    }),
    POST: vi.fn().mockImplementation(() => Promise.resolve(postResult)),
    PUT: vi.fn().mockImplementation(() => Promise.resolve(putResult)),
    DELETE: vi.fn().mockImplementation(() => Promise.resolve(deleteResult)),
    PATCH: vi.fn().mockImplementation(() => Promise.resolve(putResult)),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import SettingsTeamsView from '../views/SettingsTeamsView.vue'
import { api } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'

function team(overrides: Record<string, unknown> = {}) {
  return {
    id: 't1',
    name: 'Engineering',
    description: 'Platform team',
    account_id: 'a1',
    member_count: 2,
    owned_resource_count: 4,
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    ...overrides,
  }
}

function member(overrides: Record<string, unknown> = {}) {
  return {
    id: 'm1',
    user_id: 'u1',
    role: 'operator',
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    ...overrides,
  }
}

async function mountView() {
  const wrapper = mount(SettingsTeamsView)
  await flushPromises()
  await nextTick()
  return wrapper
}

function teamCard(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.card').find((c) => c.text().includes('Engineering'))!
}

function actionButton(scope: { findAll: (sel: string) => { text: () => string; trigger: (e: string) => Promise<void> }[] }, label: string) {
  return scope.findAll('button').find((b) => b.text().trim() === label)
}

describe('SettingsTeamsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    usePlanStore().currentTier = 'team'
    mockTeams = []
    mockUsers = []
    mockMembersByTeam = {}
    mockListError = undefined
    mockMembersError = undefined
    postResult = { data: null, error: undefined }
    putResult = { data: null, error: undefined }
    deleteResult = { response: { status: 204, ok: true }, error: undefined }
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = await mountView()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Teams')
    wrapper.unmount()
  })

  it('shows owned resource count for each team', async () => {
    mockTeams = [
      team(),
      team({ id: 't2', name: 'Design', member_count: 0, owned_resource_count: 1 }),
    ]
    const wrapper = await mountView()
    await vi.waitFor(() => {
      const counts = wrapper.findAll('[data-testid="settings-teams-owned-resource-count"]')
      expect(counts.length).toBe(2)
    })

    const counts = wrapper.findAll('[data-testid="settings-teams-owned-resource-count"]')
    expect(counts[0].text()).toContain('4')
    expect(counts[1].text()).toContain('1')
    wrapper.unmount()
  })

  it('renders the team disclosure toggle as a native button with full disclosure semantics', async () => {
    mockTeams = [team()]
    const wrapper = await mountView()

    await vi.waitFor(() => expect(wrapper.find('[data-testid="settings-teams-toggle-t1"]').exists()).toBe(true))
    const toggle = wrapper.find('[data-testid="settings-teams-toggle-t1"]')

    expect(toggle.element.tagName).toBe('BUTTON')
    expect(toggle.attributes('type')).toBe('button')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.attributes('aria-controls')).toBe('settings-teams-panel-t1')

    await toggle.trigger('click')
    await nextTick()

    const panel = wrapper.find('#settings-teams-panel-t1')
    expect(panel.exists()).toBe(true)
    expect(panel.element.tagName).toBe('SECTION')
    expect(panel.attributes('aria-label')).toBe('Engineering details')
    wrapper.unmount()
  })

  it('shows the loading spinner before the team list resolves', async () => {
    ;(api.GET as ReturnType<typeof vi.fn>).mockImplementationOnce((url: string) => {
      if (url === '/api/v1/admin/teams') return new Promise(() => {})
      return Promise.resolve(res({ items: [] }))
    })
    const wrapper = mount(SettingsTeamsView)
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows an inline error when the team list fails to load', async () => {
    mockListError = { detail: 'teams_500' }
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('teams_500')
    wrapper.unmount()
  })

  it('creates a team end-to-end and refreshes the list', async () => {
    mockTeams = []
    postResult = { data: { id: 't9', name: 'Support' }, error: undefined }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-create-team"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('New Team')

    // submit is disabled while the name is empty
    const submit = wrapper.find('[data-testid="settings-teams-create-submit"]')
    expect(submit.attributes('disabled')).toBeDefined()

    await wrapper.find('[data-testid="settings-teams-create-name"]').setValue('Support')
    await wrapper.find('[data-testid="settings-teams-create-description"]').setValue('Customer care')
    await nextTick()
    await wrapper.find('[data-testid="settings-teams-create-submit"]').trigger('click')
    await flushPromises()
    await nextTick()

    const post = vi.mocked(api.POST).mock.calls[0]
    expect(post[0]).toBe('/api/v1/admin/teams')
    expect((post[1] as any).body).toEqual({ name: 'Support', description: 'Customer care' })
    expect(wrapper.text()).toContain('Team "Support" created.')

    // reload re-issued with the new team
    mockTeams = [team({ id: 't9', name: 'Support', member_count: 0 })]
    await flushPromises()
    await nextTick()
    wrapper.unmount()
  })

  it('shows a creation error and keeps the form open when POST fails', async () => {
    postResult = { data: null, error: { detail: 'team_name_taken' } }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-create-team"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="settings-teams-create-name"]').setValue('Support')
    await nextTick()
    await wrapper.find('[data-testid="settings-teams-create-submit"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('team_name_taken')
    // form stays open with values intact
    expect(wrapper.find('[data-testid="settings-teams-create-name"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('cancel clears the create form', async () => {
    const wrapper = await mountView()
    await wrapper.find('[data-testid="settings-teams-create-team"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="settings-teams-create-name"]').setValue('Support')
    await nextTick()
    await wrapper.find('[data-testid="settings-teams-create-cancel"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="settings-teams-create-name"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the empty state when there are no teams yet', async () => {
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('No teams yet')
    wrapper.unmount()
  })

  it('renames a team and refreshes the list', async () => {
    mockTeams = [team()]
    putResult = { data: { ...team(), name: 'Platform Engineering' }, error: undefined }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await nextTick()

    const rename = actionButton(teamCard(wrapper), 'Rename')
    expect(rename).toBeTruthy()
    await rename!.trigger('click')
    await nextTick()

    const input = wrapper.find('[data-testid="settings-teams-rename-name"]')
    expect(input.exists()).toBe(true)
    expect((input.element as HTMLInputElement).value).toBe('Engineering')

    await input.setValue('Platform Engineering')
    await wrapper.find('[data-testid="settings-teams-rename-save"]').trigger('click')
    await flushPromises()
    await nextTick()

    const put = vi.mocked(api.PUT).mock.calls[0]
    expect(put[0]).toBe('/api/v1/admin/teams/{team_id}')
    expect((put[1] as any).params.path.team_id).toBe('t1')
    expect((put[1] as any).body).toEqual({
      name: 'Platform Engineering',
      expected_updated_at: '2025-01-01T00:00:00Z',
    })
    wrapper.unmount()
  })

  it('shows a rename failure inline', async () => {
    mockTeams = [team()]
    putResult = { data: null, error: { detail: 'conflict_updated_elsewhere' } }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await actionButton(teamCard(wrapper), 'Rename')!.trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="settings-teams-rename-save"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Rename failed:')
    expect(wrapper.text()).toContain('conflict_updated_elsewhere')
    wrapper.unmount()
  })

  it('deletes a team after confirmation and reloads', async () => {
    mockTeams = [team()]
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await nextTick()

    const del = actionButton(teamCard(wrapper), 'Delete')
    await del!.trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('Delete "Engineering"?')
    expect(wrapper.text()).toContain('This action cannot be undone')

    await wrapper.find('[data-testid="settings-teams-delete-confirm"]').trigger('click')
    await flushPromises()
    await nextTick()

    const delCall = vi.mocked(api.DELETE).mock.calls[0]
    expect(delCall[0]).toBe('/api/v1/admin/teams/{team_id}')
    expect((delCall[1] as any).params.path.team_id).toBe('t1')
    // panel closed after successful delete
    expect(wrapper.find('#settings-teams-panel-t1').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows a delete failure inline and keeps the confirmation open', async () => {
    mockTeams = [team()]
    deleteResult = { response: { status: 409, ok: false }, error: { detail: 'team_has_owned_resources' } }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await actionButton(teamCard(wrapper), 'Delete')!.trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="settings-teams-delete-confirm"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('team_has_owned_resources')
    expect(wrapper.find('[data-testid="settings-teams-delete-confirm"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('loads and renders members with resolved user display names and emails', async () => {
    mockTeams = [team()]
    mockUsers = [
      { id: 'u1', display_name: 'Duncan Tait', email: 'duncan@example.com' },
      { id: 'u2', display_name: 'Ada Lovelace', email: 'ada@example.com' },
    ]
    mockMembersByTeam = { t1: [member(), member({ id: 'm2', user_id: 'u2', role: 'viewer' })] }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await nextTick()

    const panel = wrapper.find('#settings-teams-panel-t1')
    expect(panel.text()).toContain('Duncan Tait')
    expect(panel.text()).toContain('duncan@example.com')
    expect(panel.text()).toContain('Ada Lovelace')
    expect(panel.text()).toContain('Members')
    wrapper.unmount()
  })

  it('shows the no-members message for an empty team', async () => {
    mockTeams = [team({ member_count: 0 })]
    mockMembersByTeam = { t1: [] }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.find('#settings-teams-panel-t1').text()).toContain('No members yet.')
    wrapper.unmount()
  })

  it('shows a members load error with a retry that recovers', async () => {
    mockTeams = [team()]
    mockUsers = [{ id: 'u1', display_name: 'Duncan Tait', email: 'duncan@example.com' }]
    mockMembersError = { detail: 'members_500' }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await nextTick()

    const panel = wrapper.find('#settings-teams-panel-t1')
    expect(panel.text()).toContain('Failed to load members:')
    expect(panel.text()).toContain('members_500')

    mockMembersError = undefined
    mockMembersByTeam = { t1: [member()] }
    const retry = wrapper.find('[data-testid="settings-teams-members-retry"]')
    await retry.trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('#settings-teams-panel-t1').text()).toContain('Duncan Tait')
    wrapper.unmount()
  })

  it('falls back to the short user id when the user is unknown', async () => {
    mockTeams = [team()]
    mockUsers = []
    mockMembersByTeam = { t1: [member({ user_id: 'u-unknown-12345678' })] }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await nextTick()

    const panel = wrapper.find('#settings-teams-panel-t1')
    expect(panel.text()).toContain('u-unknow')
    wrapper.unmount()
  })

  it('adds a member and appends them to the members table', async () => {
    mockTeams = [team()]
    mockUsers = [{ id: 'u3', display_name: 'Grace Hopper', email: 'grace@example.com' }]
    mockMembersByTeam = { t1: [member()] }
    postResult = { data: { id: 'm3', user_id: 'u3', role: 'viewer' }, error: undefined }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await nextTick()

    const add = wrapper.find('[data-testid="settings-teams-add-member"]')
    await add.trigger('click')
    await nextTick()

    const userSelect = wrapper.find('[data-testid="settings-teams-add-member-user"]')
    const roleSelect = wrapper.find('[data-testid="settings-teams-add-member-role"]')
    expect(userSelect.exists()).toBe(true)
    expect(roleSelect.exists()).toBe(true)

    // primevue Select: drive the v-model through the component directly
    const userSelectComp = userSelect.findComponent({ name: 'Select' })
    await (userSelectComp.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'u3')
    await nextTick()

    const submit = wrapper.find('[data-testid="settings-teams-add-member-submit"]')
    expect(submit.attributes('disabled')).toBeUndefined()
    await submit.trigger('click')
    await flushPromises()
    await nextTick()

    const post = vi.mocked(api.POST).mock.calls[0]
    expect(post[0]).toBe('/api/v1/teams/{team_id}/members')
    expect((post[1] as any).body).toEqual({ user_id: 'u3', role: 'viewer' })
    // the members table now shows Grace
    expect(wrapper.find('#settings-teams-panel-t1').text()).toContain('Grace Hopper')
    wrapper.unmount()
  })

  it('shows an add-member failure inline', async () => {
    mockTeams = [team()]
    mockUsers = [{ id: 'u3', display_name: 'Grace Hopper', email: 'grace@example.com' }]
    mockMembersByTeam = { t1: [] }
    postResult = { data: null, error: { detail: 'already_a_member' } }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="settings-teams-add-member"]').trigger('click')
    await nextTick()
    const userSelectComp = wrapper.find('[data-testid="settings-teams-add-member-user"]').findComponent({ name: 'Select' })
    await (userSelectComp.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'u3')
    await nextTick()
    await wrapper.find('[data-testid="settings-teams-add-member-submit"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Add member failed:')
    expect(wrapper.text()).toContain('already_a_member')
    wrapper.unmount()
  })

  it('changes a member role and swaps in the updated membership', async () => {
    mockTeams = [team()]
    mockMembersByTeam = { t1: [member()] }
    putResult = { data: { ...member(), role: 'runner' }, error: undefined }
    const wrapper = await mountView()
    const vm = wrapper.vm as unknown as { changeMemberRole?: (teamId: string, m: Record<string, unknown>) => Promise<void> }

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await nextTick()

    await vm.changeMemberRole!('t1', member())
    await flushPromises()
    await nextTick()

    const patch = vi.mocked(api.PATCH).mock.calls[0]
    expect(patch[0]).toBe('/api/v1/teams/{team_id}/members/{membership_id}')
    expect((patch[1] as any).body).toEqual({ role: 'operator' })
    wrapper.unmount()
  })

  it('shows a role-change failure and reloads the members list', async () => {
    mockTeams = [team()]
    mockMembersByTeam = { t1: [member()] }
    putResult = { data: null, error: { detail: 'role_change_denied' } }
    const wrapper = await mountView()
    const vm = wrapper.vm as unknown as { changeMemberRole?: (teamId: string, m: Record<string, unknown>) => Promise<void> }

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await nextTick()

    await vm.changeMemberRole!('t1', member())
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Role change failed:')
    expect(wrapper.text()).toContain('role_change_denied')
    wrapper.unmount()
  })

  it('removes a member and drops them from the members table', async () => {
    mockTeams = [team()]
    mockMembersByTeam = { t1: [member(), member({ id: 'm2', user_id: 'u2' })] }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await nextTick()

    const panel = wrapper.find('#settings-teams-panel-t1')
    // each member row carries a Remove action
    const removeBtns = panel.findAll('button').filter((b) => b.text().trim() === 'Remove')
    expect(removeBtns.length).toBe(2)
    await removeBtns[0].trigger('click')
    await flushPromises()
    await nextTick()

    const delCall = vi.mocked(api.DELETE).mock.calls[0]
    expect(delCall[0]).toBe('/api/v1/teams/{team_id}/members/{membership_id}')
    expect((delCall[1] as any).params.path).toEqual({ team_id: 't1', membership_id: 'm1' })
    wrapper.unmount()
  })

  it('shows a remove failure inline', async () => {
    mockTeams = [team()]
    mockMembersByTeam = { t1: [member()] }
    deleteResult = { response: { status: 403, ok: false }, error: { detail: 'remove_denied' } }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await nextTick()

    const removeBtns = wrapper.find('#settings-teams-panel-t1').findAll('button').filter((b) => b.text().trim() === 'Remove')
    await removeBtns[0].trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Remove failed:')
    expect(wrapper.text()).toContain('remove_denied')
    wrapper.unmount()
  })

  it('BUG: member_count on the team header does not update after add/remove (readonly vue-query data)', async () => {
    // Production bug characterisation. addMember()/removeMember() adjust
    // `team.member_count` on items that come from @tanstack/vue-query's
    // deep-readonly query state; Vue drops the write, so the header count
    // stays stale until the next full list reload.
    mockTeams = [team({ member_count: 1 })]
    mockUsers = [{ id: 'u3', display_name: 'Grace Hopper', email: 'grace@example.com' }]
    mockMembersByTeam = { t1: [] }
    postResult = { data: { id: 'm3', user_id: 'u3', role: 'viewer' }, error: undefined }
    const wrapper = await mountView()

    await wrapper.find('[data-testid="settings-teams-toggle-t1"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="settings-teams-add-member"]').trigger('click')
    await nextTick()
    const userSelectComp = wrapper.find('[data-testid="settings-teams-add-member-user"]').findComponent({ name: 'Select' })
    await (userSelectComp.vm as unknown as { $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'u3')
    await nextTick()
    await wrapper.find('[data-testid="settings-teams-add-member-submit"]').trigger('click')
    await flushPromises()
    await nextTick()

    // member appended to the table but the header count still reads the stale "1 member"
    expect(wrapper.find('#settings-teams-panel-t1').text()).toContain('Grace Hopper')
    expect(teamCard(wrapper).text()).toContain('1 member')
    wrapper.unmount()
  })
})
