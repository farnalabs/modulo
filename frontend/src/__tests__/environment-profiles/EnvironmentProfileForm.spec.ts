import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const routeState = vi.hoisted(() => ({ params: {} as Record<string, string> }))

const { getMock, postMock, putMock, delMock, routerPush } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  putMock: vi.fn(),
  delMock: vi.fn(),
  routerPush: vi.fn(),
}))

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: getMock, post: postMock, put: putMock, delete: delMock, patch: vi.fn() }),
}))

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({
    path: '/environment-profiles/new',
    fullPath: '/environment-profiles/new',
    params: routeState.params,
    query: {},
    hash: '',
    matched: [],
    name: null,
    redirectedFrom: undefined,
    meta: {},
  })),
  useRouter: vi.fn(() => ({ push: routerPush, replace: vi.fn() })),
  createRouter: vi.fn(),
  createWebHistory: vi.fn(() => ({})),
}))

import EnvironmentProfileForm from '../../views/environment-profiles/EnvironmentProfileForm.vue'

function fullProfile(over: Record<string, unknown> = {}) {
  return {
    id: 'prof-1',
    name: 'Existing Profile',
    description: 'A stored profile',
    provider_type: 'e2b',
    image_ref: 'python:3.12-slim',
    capabilities: ['git', 'shell'],
    network_policy: 'none',
    initialisation_strategy: 'blank',
    persistence_policy: 'retained',
    status: 'active',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

function mountForm() {
  return mount(EnvironmentProfileForm, {
    global: {
      mocks: { $router: { push: routerPush } },
    },
  })
}

async function flush() {
  await flushPromises()
  await nextTick()
}

describe('EnvironmentProfileForm — create mode', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    routeState.params = {}
  })

  it('renders the create form and does not fetch a profile when id is "new"', async () => {
    const wrapper = mountForm()
    await flush()

    expect(wrapper.text()).toContain('New Environment Profile')
    expect(getMock).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="envprofile-form-name"]').exists()).toBe(true)
  })

  it('submit without a name shows the required error and never calls the API', async () => {
    const wrapper = mountForm()
    await flush()

    await wrapper.find('form').trigger('submit')
    await flush()

    expect(wrapper.text()).toContain('Name is required')
    expect(postMock).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="envprofile-form-name"]').classes()).toContain('border-destructive')
  })

  it('create: valid submit POSTs the exact snake_case payload and navigates back to the list', async () => {
    postMock.mockResolvedValue(fullProfile({ name: 'python-dev' }))
    const wrapper = mountForm()
    await flush()

    await wrapper.find('[data-testid="envprofile-form-name"]').setValue('python-dev')
    await wrapper.find('[data-testid="envprofile-form-description"]').setValue('  dev sandbox  ')
    await wrapper.find('[data-testid="envprofile-form-image"]').setValue('python:3.12-slim')
    await wrapper.find('form').trigger('submit')
    await flush()

    expect(postMock).toHaveBeenCalledTimes(1)
    const [path, payload] = postMock.mock.calls[0]
    expect(path).toBe('/api/v1/environment-profiles')
    expect(payload).toEqual({
      name: 'python-dev',
      description: 'dev sandbox',
      provider_type: 'local_docker',
      image_ref: 'python:3.12-slim',
      capabilities: [],
      network_policy: 'outbound',
      initialisation_strategy: 'git_clone',
      persistence_policy: 'ephemeral',
    })
    expect(routerPush).toHaveBeenCalledWith('/environment-profiles')
  })

  it('capabilities toggle on and off via the checkboxes and land in the payload', async () => {
    postMock.mockResolvedValue(fullProfile())
    const wrapper = mountForm()
    await flush()

    await wrapper.find('[data-testid="envprofile-form-name"]').setValue('cap-profile')
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(6)

    // Toggle 'git' (index 0) on and 'shell' (index 3) on, then 'shell' off again.
    await wrapper.findAll('input[type="checkbox"]')[0].trigger('change')
    await wrapper.findAll('input[type="checkbox"]')[3].trigger('change')
    await wrapper.findAll('input[type="checkbox"]')[3].trigger('change')

    const vm = wrapper.vm as unknown as { form: { capabilities: string[] } }
    expect(vm.form.capabilities).toEqual(['git'])

    await wrapper.find('form').trigger('submit')
    await flush()

    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock.mock.calls[0][1].capabilities).toEqual(['git'])
  })

  it('shows the tier badge once a provider with a runner tier is selected', async () => {
    const wrapper = mountForm()
    await flush()

    expect(wrapper.find('[data-testid="envprofile-form-tier-badge"]').exists()).toBe(true)

    const vm = wrapper.vm as unknown as { form: { provider_type: string } }
    vm.form.provider_type = 'e2b'
    await nextTick()

    const badge = wrapper.find('[data-testid="envprofile-form-tier-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text().length).toBeGreaterThan(0)
  })

  it('create failure surfaces the store error message inline and stays on the form', async () => {
    postMock.mockRejectedValue(new Error('quota exhausted'))
    const wrapper = mountForm()
    await flush()

    await wrapper.find('[data-testid="envprofile-form-name"]').setValue('doomed')
    await wrapper.find('form').trigger('submit')
    await flush()

    expect(wrapper.text()).toContain('quota exhausted')
    expect(routerPush).not.toHaveBeenCalled()
  })

  it('back and cancel buttons navigate back to the list', async () => {
    const wrapper = mountForm()
    await flush()

    await wrapper.find('[data-testid="envprofile-form-back"]').trigger('click')
    expect(routerPush).toHaveBeenCalledWith('/environment-profiles')

    await wrapper.find('[data-testid="envprofile-form-cancel"]').trigger('click')
    expect(routerPush).toHaveBeenCalledWith('/environment-profiles')
  })
})

describe('EnvironmentProfileForm — edit mode', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    routeState.params = { id: 'prof-1' }
  })

  async function mountEdit() {
    getMock.mockResolvedValue(fullProfile())
    const wrapper = mountForm()
    await flush()
    return wrapper
  }

  it('fetches the profile on mount and prefills every form field', async () => {
    const wrapper = await mountEdit()

    expect(getMock).toHaveBeenCalledWith('/api/v1/environment-profiles/prof-1')
    expect(wrapper.text()).toContain('Edit Environment Profile')
    expect((wrapper.find('[data-testid="envprofile-form-name"]').element as HTMLInputElement).value).toBe('Existing Profile')
    expect((wrapper.find('[data-testid="envprofile-form-description"]').element as HTMLTextAreaElement).value).toBe('A stored profile')
    expect((wrapper.find('[data-testid="envprofile-form-image"]').element as HTMLInputElement).value).toBe('python:3.12-slim')

    const vm = wrapper.vm as unknown as { form: Record<string, unknown> }
    expect(vm.form.provider_type).toBe('e2b')
    expect(vm.form.capabilities).toEqual(['git', 'shell'])
    expect(vm.form.network_policy).toBe('none')
    expect(vm.form.initialisation_strategy).toBe('blank')
    expect(vm.form.persistence_policy).toBe('retained')
  })

  it('edit: submit PUTs the profile id with the edited fields', async () => {
    putMock.mockResolvedValue(fullProfile({ name: 'Renamed' }))
    const wrapper = await mountEdit()

    await wrapper.find('[data-testid="envprofile-form-name"]').setValue('Renamed')
    await wrapper.find('form').trigger('submit')
    await flush()

    expect(putMock).toHaveBeenCalledTimes(1)
    const [path, payload] = putMock.mock.calls[0]
    expect(path).toBe('/api/v1/environment-profiles/prof-1')
    expect(payload.name).toBe('Renamed')
    expect(payload.provider_type).toBe('e2b')
    expect(routerPush).toHaveBeenCalledWith('/environment-profiles')
  })

  it('edit: fetch failure shows the store error and leaves the form blank', async () => {
    getMock.mockRejectedValue(new Error('profile not found'))
    const wrapper = mountForm()
    await flush()

    expect(wrapper.text()).toContain('profile not found')
    const vm = wrapper.vm as unknown as { form: Record<string, unknown> }
    expect(vm.form.name).toBe('')
    expect(postMock).not.toHaveBeenCalled()
  })
})
