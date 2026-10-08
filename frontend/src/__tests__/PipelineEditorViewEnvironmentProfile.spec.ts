// FAR-1558 slice 2: the pipeline editor's per-pipeline environment-profile
// selector. Pins the omit-vs-null contract (untouched = no request, "" =
// explicit null clear), the provider-tier option labels, the plan-feature
// gate on the list fetch, the unresolvable-binding placeholder, and the
// a11y roles on the selector's async feedback.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, computed } from 'vue'

const state = vi.hoisted(() => ({
  pipeline: {} as Record<string, unknown>,
  profiles: [] as Array<{ id: string; name: string; provider_type: string }>,
  profileListFetches: 0,
  profileListError: false,
  pipelineLoadFails: false,
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn((url: string) => {
      if (url.includes('/api/v1/environment-profiles')) {
        state.profileListFetches += 1
        if (state.profileListError) return Promise.reject(new Error('profiles unavailable'))
        return Promise.resolve({ items: state.profiles })
      }
      return Promise.resolve({ items: [] })
    }),
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
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({ data: { nodes: [], edges: [] }, error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}')) {
      if (state.pipelineLoadFails) {
        return Promise.resolve({ data: undefined, error: { detail: 'not found' } })
      }
      return Promise.resolve({ data: { ...state.pipeline }, error: undefined })
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
import { usePlanStore } from '../stores/planStore'

const MESSAGES = enUS as unknown as {
  views: { PipelineEditorView: Record<string, string> }
  components: { RunnerTier: Record<string, string> }
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView },
    { path: '/library', name: 'library', component: { template: '<div />' } },
  ],
})

const SELECT = '[data-testid="pipeline-editor-environment-profile"]'
const ERROR = '[data-testid="pipeline-editor-environment-profile-error"]'
const STATUS = '[data-testid="pipeline-editor-environment-profile-status"]'
const LABEL = 'label[for="pipeline-environment-profile"]'

const PROFILES = [
  { id: 'prof-docker', name: 'Staging Docker', provider_type: 'runner_docker' },
  { id: 'prof-e2b', name: 'Cloud Sandbox', provider_type: 'e2b' },
]

function defaultPipeline(): Record<string, unknown> {
  return {
    id: 'test-pipeline-id',
    name: 'Test Pipeline',
    environment_profile_id: null,
  }
}

async function mountEditor(options: { featureEnabled?: boolean } = {}) {
  router.push('/pipelines/test-pipeline-id/editor')
  await router.isReady()
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.currentTier = options.featureEnabled ? 'team' : 'community'
  store.features = options.featureEnabled ? { environment_profiles: true } : {}
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

function environmentProfilePatchBodies(): unknown[] {
  return vi
    .mocked(api.PATCH)
    .mock.calls
    .filter((c) => c[0] === '/api/v1/pipelines/{pipeline_id}')
    .map((c) => (c[1] as { body?: Record<string, unknown> }).body)
    .filter((body) => body !== undefined && 'environment_profile_id' in body)
}

function optionsOf(wrapper: Awaited<ReturnType<typeof mountEditor>>) {
  return wrapper.find(SELECT).findAll('option')
}

beforeEach(async () => {
  vi.clearAllMocks()
  state.pipeline = defaultPipeline()
  state.profiles = []
  state.profileListFetches = 0
  state.profileListError = false
  state.pipelineLoadFails = false
  const { useRoute } = await import('vue-router')
  const route = (useRoute as unknown as () => { params: Record<string, string> })()
  route.params = { id: 'test-pipeline-id' }
})

describe('PipelineEditorView - environment profile selector', () => {
  it('is hidden and never fetches the list while the plan lacks the environment_profiles feature', async () => {
    const wrapper = await mountEditor()

    expect(wrapper.find(SELECT).exists()).toBe(false)
    expect(state.profileListFetches).toBe(0)
    wrapper.unmount()
  })

  it('labels the control from locale keys and associates the label with the select', async () => {
    state.profiles = PROFILES
    const wrapper = await mountEditor({ featureEnabled: true })

    const label = wrapper.find(LABEL)
    expect(label.exists()).toBe(true)
    expect(label.text()).toBe(`${MESSAGES.views.PipelineEditorView.environment_profile_label}:`)
    expect(wrapper.find(SELECT).attributes('aria-describedby')).toBe('pipeline-environment-profile-help')
    expect(wrapper.find('#pipeline-environment-profile-help').exists()).toBe(true)
    wrapper.unmount()
  })

  it('lists the org profiles with their provider tier and offers the none option', async () => {
    state.profiles = PROFILES
    const wrapper = await mountEditor({ featureEnabled: true })

    expect(state.profileListFetches).toBe(1)
    const options = optionsOf(wrapper)
    expect(options).toHaveLength(3)
    expect(options[0].text()).toBe(MESSAGES.views.PipelineEditorView.environment_profile_none)
    expect(options[1].text()).toBe(
      `Staging Docker — ${MESSAGES.components.RunnerTier.bundled_docker}`,
    )
    expect(options[2].text()).toBe(`Cloud Sandbox — ${MESSAGES.components.RunnerTier.external_e2b}`)
    wrapper.unmount()
  })

  it('shows the stored binding as the selected value', async () => {
    state.pipeline = { ...defaultPipeline(), environment_profile_id: 'prof-e2b' }
    state.profiles = PROFILES
    const wrapper = await mountEditor({ featureEnabled: true })

    expect((wrapper.find(SELECT).element as HTMLSelectElement).value).toBe('prof-e2b')
    wrapper.unmount()
  })

  it('binds a chosen profile through the existing pipeline PATCH and announces it', async () => {
    state.profiles = PROFILES
    const wrapper = await mountEditor({ featureEnabled: true })

    await wrapper.find(SELECT).setValue('prof-docker')
    await flushPromises()

    expect(environmentProfilePatchBodies()).toEqual([{ environment_profile_id: 'prof-docker' }])

    const status = wrapper.find(STATUS)
    expect(status.exists()).toBe(true)
    expect(status.attributes('role')).toBe('status')
    expect(status.text()).toContain('Staging Docker')
    expect(status.text()).toContain(MESSAGES.components.RunnerTier.bundled_docker)
    wrapper.unmount()
  })

  it('sends an explicit null to clear the binding and restore the default route', async () => {
    state.pipeline = { ...defaultPipeline(), environment_profile_id: 'prof-docker' }
    state.profiles = PROFILES
    const wrapper = await mountEditor({ featureEnabled: true })

    await wrapper.find(SELECT).setValue('')
    await flushPromises()

    expect(environmentProfilePatchBodies()).toEqual([{ environment_profile_id: null }])
    expect(wrapper.find(ERROR).exists()).toBe(false)
    expect(wrapper.find(STATUS).text()).toBe(
      MESSAGES.views.PipelineEditorView.environment_profile_cleared,
    )
    wrapper.unmount()
  })

  it('sends nothing while the control is untouched (the binding stays omitted)', async () => {
    state.pipeline = { ...defaultPipeline(), environment_profile_id: 'prof-docker' }
    state.profiles = PROFILES
    const wrapper = await mountEditor({ featureEnabled: true })

    await flushPromises()

    expect(environmentProfilePatchBodies()).toHaveLength(0)
    wrapper.unmount()
  })

  it('surfaces a refused bind as an alert and reverts to the stored value', async () => {
    state.pipeline = { ...defaultPipeline(), environment_profile_id: 'prof-docker' }
    state.profiles = PROFILES
    vi.mocked(api.PATCH).mockImplementationOnce(() =>
      Promise.resolve({
        data: undefined,
        error: { detail: 'environment_profile_binding_team_mismatch' },
      } as never),
    )
    const wrapper = await mountEditor({ featureEnabled: true })

    await wrapper.find(SELECT).setValue('prof-e2b')
    await flushPromises()

    const error = wrapper.find(ERROR)
    expect(error.exists()).toBe(true)
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toContain('environment_profile_binding_team_mismatch')
    expect((wrapper.find(SELECT).element as HTMLSelectElement).value).toBe('prof-docker')
    expect(wrapper.find(STATUS).exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps an unresolvable binding visible as an option instead of showing none', async () => {
    state.pipeline = { ...defaultPipeline(), environment_profile_id: 'prof-foreign' }
    state.profiles = PROFILES
    const wrapper = await mountEditor({ featureEnabled: true })

    const select = wrapper.find(SELECT)
    expect((select.element as HTMLSelectElement).value).toBe('prof-foreign')
    const foreign = optionsOf(wrapper).find(
      (o) => (o.element as HTMLOptionElement).value === 'prof-foreign',
    )
    expect(foreign).toBeDefined()
    expect(foreign?.text()).toBe(MESSAGES.views.PipelineEditorView.environment_profile_unavailable)
    wrapper.unmount()
  })

  it('renders a profile with no resolvable tier by its name alone (no dangling separator)', async () => {
    state.profiles = [{ id: 'prof-plain', name: 'Plain Profile', provider_type: 'mystery_provider' }]
    const wrapper = await mountEditor({ featureEnabled: true })

    const plain = optionsOf(wrapper).find(
      (o) => (o.element as HTMLOptionElement).value === 'prof-plain',
    )
    expect(plain).toBeDefined()
    expect(plain?.text()).toBe('Plain Profile')
    wrapper.unmount()
  })

  it('surfaces a profile-list load failure as an alert instead of a silent empty menu', async () => {
    state.profileListError = true
    const wrapper = await mountEditor({ featureEnabled: true })

    const error = wrapper.find(ERROR)
    expect(error.exists()).toBe(true)
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toContain('profiles unavailable')
    wrapper.unmount()
  })

  it('falls back to the raw id in the bound status when the profile is not in the list', async () => {
    state.pipeline = { ...defaultPipeline(), environment_profile_id: 'prof-foreign' }
    state.profiles = PROFILES
    const wrapper = await mountEditor({ featureEnabled: true })

    await wrapper.find(SELECT).setValue('prof-foreign')
    await flushPromises()

    expect(environmentProfilePatchBodies()).toEqual([{ environment_profile_id: 'prof-foreign' }])
    const status = wrapper.find(STATUS)
    expect(status.exists()).toBe(true)
    expect(status.text()).toContain('prof-foreign')
    wrapper.unmount()
  })

  it('binds successfully even when the pipeline record is not loaded (null-safe assignment)', async () => {
    state.pipelineLoadFails = true
    state.profiles = PROFILES
    const wrapper = await mountEditor({ featureEnabled: true })

    await wrapper.find(SELECT).setValue('prof-docker')
    await flushPromises()

    expect(environmentProfilePatchBodies()).toEqual([{ environment_profile_id: 'prof-docker' }])
    const status = wrapper.find(STATUS)
    expect(status.exists()).toBe(true)
    expect(status.text()).toContain('Staging Docker')
    wrapper.unmount()
  })

  it('reverts and alerts when the bind write throws (timeout/network)', async () => {
    state.pipeline = { ...defaultPipeline(), environment_profile_id: 'prof-docker' }
    state.profiles = PROFILES
    vi.mocked(api.PATCH).mockImplementationOnce(() => Promise.reject(new Error('network down')))
    const wrapper = await mountEditor({ featureEnabled: true })

    await wrapper.find(SELECT).setValue('prof-e2b')
    await flushPromises()

    const error = wrapper.find(ERROR)
    expect(error.exists()).toBe(true)
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toContain('network down')
    expect((wrapper.find(SELECT).element as HTMLSelectElement).value).toBe('prof-docker')
    wrapper.unmount()
  })
})
