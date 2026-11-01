// FAR-2279: the pipeline editor's owner-team selector. Pins the team list,
// the None (org-wide) option, the PATCH wire shapes (id / explicit null) and
// loud failure on a refused write.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, computed } from 'vue'

const state = vi.hoisted(() => ({
  pipeline: {} as Record<string, unknown> | null,
  teams: [] as Array<{ id: string; name: string }>,
  teamsError: null as { detail?: string } | null,
  teamsRejects: false,
  teamsEmptyBody: false,
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn().mockResolvedValue({ items: [] }),
    post: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue({}),
  }),
}))

vi.mock('../composables/useCurrentUser', () => ({
  useCurrentUser: () => ({
    jwtPayload: computed(() => null),
    userId: computed(() => 'user-1'),
    orgId: computed(() => 'org-1'),
    orgRole: computed(() => 'admin'),
    isSystemAdmin: computed(() => false),
    isOperator: computed(() => true),
    permissions: computed(() => null),
  }),
}))

vi.mock('../lib/api/client', () => {
  const get = (url: string) => {
    if (url === '/api/v1/teams') {
      if (state.teamsRejects) {
        return Promise.reject(new Error('teams endpoint down'))
      }
      if (state.teamsError) {
        return Promise.resolve({ data: undefined, error: state.teamsError })
      }
      if (state.teamsEmptyBody) {
        return Promise.resolve({ data: undefined, error: undefined })
      }
      return Promise.resolve({ data: { items: state.teams, total: state.teams.length }, error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({ data: { nodes: [], edges: [] }, error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}')) {
      return Promise.resolve({ data: state.pipeline ? { ...state.pipeline } : null, error: undefined })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  }
  return {
    api: {
      GET: vi.fn(get),
      POST: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
      PATCH: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
      PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
      DELETE: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    },
    getAccessToken: vi.fn().mockReturnValue('mock-token'),
  }
})

import { api } from '../lib/api/client'
import enUS from '../locales/en-US.js'
import PipelineEditorView from '../views/PipelineEditorView.vue'

const MESSAGES = enUS as unknown as { views: { PipelineEditorView: Record<string, string> } }

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView },
    { path: '/library', name: 'library', component: { template: '<div />' } },
  ],
})

const SELECT = '[data-testid="pipeline-editor-owner-team"]'
const ERROR = '[data-testid="pipeline-editor-owner-team-error"]'
const STATUS = '[data-testid="pipeline-editor-owner-team-status"]'

const TEAMS = [
  { id: 'team-a', name: 'Platform' },
  { id: 'team-b', name: 'Growth' },
]

async function mountEditor() {
  router.push('/pipelines/test-pipeline-id/editor')
  await router.isReady()
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(PipelineEditorView, {
    global: {
      plugins: [pinia, router],
      stubs: { VueFlow: { template: '<div><slot /></div>' }, Background: true, Controls: true },
    },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

function ownerTeamPatchBodies(): unknown[] {
  return vi
    .mocked(api.PATCH)
    .mock.calls.filter((c) => c[0] === '/api/v1/pipelines/{pipeline_id}')
    .map((c) => (c[1] as { body?: Record<string, unknown> }).body)
    .filter((body) => body !== undefined && 'owner_team_id' in body)
}

beforeEach(async () => {
  vi.clearAllMocks()
  state.pipeline = { id: 'test-pipeline-id', name: 'Test Pipeline', owner_team_id: null }
  state.teams = TEAMS
  state.teamsError = null
  state.teamsRejects = false
  state.teamsEmptyBody = false
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

describe('PipelineEditorView - owner team selector', () => {
  it('lists the visible teams after the None (org-wide) option', async () => {
    const wrapper = await mountEditor()

    const options = wrapper.find(SELECT).findAll('option')
    expect(options.map((o) => o.text())).toEqual([
      MESSAGES.views.PipelineEditorView.owner_team_none,
      'Platform',
      'Growth',
    ])
    wrapper.unmount()
  })

  it('shows the stored owner team as the selected value', async () => {
    state.pipeline = { ...state.pipeline, owner_team_id: 'team-b' }
    const wrapper = await mountEditor()

    expect((wrapper.find(SELECT).element as HTMLSelectElement).value).toBe('team-b')
    wrapper.unmount()
  })

  it('sends nothing while untouched', async () => {
    const wrapper = await mountEditor()

    expect(ownerTeamPatchBodies()).toHaveLength(0)
    wrapper.unmount()
  })

  it('PATCHes owner_team_id when a team is chosen and announces it', async () => {
    const wrapper = await mountEditor()

    await wrapper.find(SELECT).setValue('team-a')
    await flushPromises()

    expect(ownerTeamPatchBodies()).toEqual([{ owner_team_id: 'team-a' }])
    expect(wrapper.find(STATUS).text()).toContain('Platform')
    expect(wrapper.find(ERROR).exists()).toBe(false)
    wrapper.unmount()
  })

  it('warns that the graph needs a re-save when the server says connectors must rebind', async () => {
    vi.mocked(api.PATCH).mockImplementationOnce(() =>
      Promise.resolve({ data: { connector_rebind_required: true }, error: undefined } as never),
    )
    const wrapper = await mountEditor()

    await wrapper.find(SELECT).setValue('team-a')
    await flushPromises()

    expect(wrapper.find(STATUS).text()).toContain(
      MESSAGES.views.PipelineEditorView.owner_team_rebind_required,
    )
    wrapper.unmount()
  })

  it('sends an explicit null when cleared to None (org-wide)', async () => {
    state.pipeline = { ...state.pipeline, owner_team_id: 'team-a' }
    const wrapper = await mountEditor()

    await wrapper.find(SELECT).setValue('')
    await flushPromises()

    expect(ownerTeamPatchBodies()).toEqual([{ owner_team_id: null }])
    expect(wrapper.find(STATUS).text()).toBe(MESSAGES.views.PipelineEditorView.owner_team_cleared)
    wrapper.unmount()
  })

  it('shows an alert and reverts the selection on a 422', async () => {
    state.pipeline = { ...state.pipeline, owner_team_id: 'team-a' }
    vi.mocked(api.PATCH).mockImplementationOnce(() =>
      Promise.resolve({
        data: undefined,
        error: { detail: 'environment_profile_binding_team_mismatch' },
      } as never),
    )
    const wrapper = await mountEditor()

    await wrapper.find(SELECT).setValue('team-b')
    await flushPromises()

    const error = wrapper.find(ERROR)
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toContain('environment_profile_binding_team_mismatch')
    expect((wrapper.find(SELECT).element as HTMLSelectElement).value).toBe('team-a')
    wrapper.unmount()
  })

  it('shows an alert when the team list cannot be loaded', async () => {
    state.teamsError = { detail: 'teams_unavailable' }
    const wrapper = await mountEditor()

    const error = wrapper.find(ERROR)
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toContain('teams_unavailable')
    // The failed list must not render as a silent empty menu: None remains.
    expect(wrapper.find(SELECT).findAll('option')).toHaveLength(1)
    wrapper.unmount()
  })

  it('shows an alert when the team request throws', async () => {
    state.teamsRejects = true
    const wrapper = await mountEditor()

    expect(wrapper.find(ERROR).text()).toContain('teams endpoint down')
    wrapper.unmount()
  })

  it('renders only the None option when the team list body is empty', async () => {
    state.teamsEmptyBody = true
    const wrapper = await mountEditor()

    expect(wrapper.find(SELECT).findAll('option')).toHaveLength(1)
    expect(wrapper.find(ERROR).exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows an alert and reverts when the write fails on the wire', async () => {
    state.pipeline = { ...state.pipeline, owner_team_id: 'team-a' }
    vi.mocked(api.PATCH).mockImplementationOnce(() => Promise.reject(new Error('network down')))
    const wrapper = await mountEditor()

    await wrapper.find(SELECT).setValue('team-b')
    await flushPromises()

    const error = wrapper.find(ERROR)
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toContain('network down')
    expect((wrapper.find(SELECT).element as HTMLSelectElement).value).toBe('team-a')
    wrapper.unmount()
  })

  it('labels a chosen team absent from the loaded list by its id', async () => {
    const wrapper = await mountEditor()
    const select = wrapper.find(SELECT)
    // The select can only offer listed teams, so an unlisted value cannot be
    // produced by a user click; pin the defensive fallback with an injected
    // option rather than rendering an empty label if it ever happens.
    const ghost = document.createElement('option')
    ghost.value = 'team-ghost'
    ghost.text = 'Ghost'
    ;(select.element as HTMLSelectElement).appendChild(ghost)

    await select.setValue('team-ghost')
    await flushPromises()

    expect(ownerTeamPatchBodies()).toEqual([{ owner_team_id: 'team-ghost' }])
    expect(wrapper.find(STATUS).text()).toBe('Owner team set to team-ghost')
    wrapper.unmount()
  })

  it('still announces the outcome when no pipeline document is loaded', async () => {
    state.pipeline = null
    const wrapper = await mountEditor()

    await wrapper.find(SELECT).setValue('team-a')
    await flushPromises()

    expect(ownerTeamPatchBodies()).toEqual([{ owner_team_id: 'team-a' }])
    expect(wrapper.find(STATUS).text()).toContain('Platform')
    expect(wrapper.find(ERROR).exists()).toBe(false)
    wrapper.unmount()
  })
})
