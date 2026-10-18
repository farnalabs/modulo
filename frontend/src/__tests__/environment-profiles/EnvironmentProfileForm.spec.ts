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
import AppSelect from '../../components/shared/AppSelect.vue'

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
    owner_team_id: null,
    visibility: 'org',
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
    // FAR-1614: the provider-required error announces itself (role="alert"),
    // mirroring the tier hint's role="status" — an error a screen reader
    // never hears is not surfaced at all.
    const providerError = wrapper.find('#envprofile-form-provider-error')
    expect(providerError.exists()).toBe(true)
    expect(providerError.attributes('role')).toBe('alert')
  })

  it('create: valid submit POSTs the exact snake_case payload and navigates back to the list', async () => {
    postMock.mockResolvedValue(fullProfile({ name: 'python-dev' }))
    const wrapper = mountForm()
    await flush()

    const vm = wrapper.vm as unknown as { form: { provider_type: string } }
    vm.form.provider_type = 'local_docker'
    await nextTick()

    await wrapper.find('[data-testid="envprofile-form-name"]').setValue('python-dev')
    await wrapper.find('[data-testid="envprofile-form-description"]').setValue('  dev sandbox  ')
    await wrapper.find('[data-testid="envprofile-form-image"]').setValue('python:3.12-slim')
    // FAR-587 made provider_type a required field with no default, so it is
    // selected explicitly above (provider_type = 'local_docker') before submit.
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
      visibility: 'org',
    })
    expect(routerPush).toHaveBeenCalledWith('/environment-profiles')
  })

  it('capabilities toggle on and off via the checkboxes and land in the payload', async () => {
    postMock.mockResolvedValue(fullProfile())
    const wrapper = mountForm()
    await flush()

    const vm = wrapper.vm as unknown as { form: { provider_type: string; capabilities: string[] } }
    vm.form.provider_type = 'local_docker'
    await nextTick()

    await wrapper.find('[data-testid="envprofile-form-name"]').setValue('cap-profile')

    const pv = wrapper.vm as unknown as { form: { provider_type: string } }
    pv.form.provider_type = 'local_docker'
    await nextTick()

    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(6)

    // FAR-587 made provider_type required (no default) - select it so submit passes.
    const vmCap = wrapper.vm as unknown as { form: { provider_type: string } }
    vmCap.form.provider_type = 'local_docker'

    // Toggle 'git' (index 0) on and 'shell' (index 3) on, then 'shell' off again.
    await wrapper.findAll('input[type="checkbox"]')[0].trigger('change')
    await wrapper.findAll('input[type="checkbox"]')[3].trigger('change')
    await wrapper.findAll('input[type="checkbox"]')[3].trigger('change')

    expect(vm.form.capabilities).toEqual(['git'])
    vm.form.provider_type = 'local_docker'
    await nextTick()

    await wrapper.find('form').trigger('submit')
    await flush()

    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock.mock.calls[0][1].capabilities).toEqual(['git'])
  })

  it('offers kubernetes as a third provider type alongside local_docker and e2b', async () => {
    const wrapper = mountForm()
    await flush()

    const providerSelect = wrapper
      .findAllComponents(AppSelect)
      .find((s) => s.find('[data-testid="envprofile-form-provider"]').exists())
    expect(providerSelect).toBeTruthy()

    const options = providerSelect!.props('options') as Array<{ value: string; label: string }>
    expect(options.map((o) => o.value)).toEqual(['local_docker', 'e2b', 'kubernetes'])
    expect(options.map((o) => o.label)).toEqual([
      'Bundled Runner (Docker)',
      'External Runner (E2B)',
      'External Runner (Kubernetes)',
    ])
  })

  it('labels the provider select and links its dynamic tier hint via aria-describedby', async () => {
    const wrapper = mountForm()
    await flush()

    const selectRoot = wrapper.find('[data-testid="envprofile-form-provider"]')
    expect(selectRoot.exists()).toBe(true)

    // The visible label's `for` resolves to the combobox's id, which carries
    // the accessible name (PrimeVue renders a span[role=combobox], not input).
    const combobox = selectRoot.find('[role="combobox"]')
    expect(combobox.exists()).toBe(true)
    expect(combobox.attributes('aria-label')).toBe('Provider Type')
    expect(combobox.attributes('id')).toBe('environmentprofileform-field-5')
    expect(wrapper.find('label[for="environmentprofileform-field-5"]').exists()).toBe(true)

    // The dynamic tier hint is a status region, referenced by the combobox.
    const vm = wrapper.vm as unknown as { form: { provider_type: string } }
    vm.form.provider_type = 'kubernetes'
    await nextTick()

    const hint = wrapper.find('#envprofile-form-tier-hint')
    expect(hint.exists()).toBe(true)
    expect(hint.attributes('role')).toBe('status')
    expect(combobox.attributes('aria-describedby')).toContain('envprofile-form-tier-hint')
  })

  it('create: selecting kubernetes POSTs provider_type kubernetes', async () => {
    postMock.mockResolvedValue(fullProfile({ provider_type: 'kubernetes' }))
    const wrapper = mountForm()
    await flush()

    const vm = wrapper.vm as unknown as { form: { provider_type: string } }
    vm.form.provider_type = 'kubernetes'
    await nextTick()

    await wrapper.find('[data-testid="envprofile-form-name"]').setValue('k8s-profile')
    await wrapper.find('form').trigger('submit')
    await flush()

    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock.mock.calls[0][1].provider_type).toBe('kubernetes')
    expect(routerPush).toHaveBeenCalledWith('/environment-profiles')
  })

  it('shows the tier badge once a provider with a runner tier is selected', async () => {
    const wrapper = mountForm()
    await flush()

    // No tier badge is shown until a provider with a runner tier is selected.
    expect(wrapper.find('[data-testid="envprofile-form-tier-badge"]').exists()).toBe(false)

    const vm = wrapper.vm as unknown as { form: { provider_type: string } }
    vm.form.provider_type = ''
    await nextTick()

    // The badge only renders once a provider with a runner tier is selected.
    expect(wrapper.find('[data-testid="envprofile-form-tier-badge"]').exists()).toBe(false)

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

    const vm = wrapper.vm as unknown as { form: { provider_type: string } }
    vm.form.provider_type = 'local_docker'
    await nextTick()

    await wrapper.find('[data-testid="envprofile-form-name"]').setValue('doomed')
    // FAR-587 made provider_type required (no default) - select it so submit reaches the API.
    const vmFail = wrapper.vm as unknown as { form: { provider_type: string } }
    vmFail.form.provider_type = 'local_docker'
    await wrapper.find('form').trigger('submit')
    await flush()

    expect(wrapper.text()).toContain('quota exhausted')
    expect(routerPush).not.toHaveBeenCalled()
  })

  it('create failure with a non-Error rejection falls back to the generic save error', async () => {
    postMock.mockRejectedValue('quota exhausted (non-Error)')
    const wrapper = mountForm()
    await flush()

    await wrapper.find('[data-testid="envprofile-form-name"]').setValue('doomed')
    // FAR-587 made provider_type required (no default) - select it so submit reaches the API.
    const vm = wrapper.vm as unknown as { form: { provider_type: string } }
    vm.form.provider_type = 'local_docker'
    await nextTick()

    await wrapper.find('form').trigger('submit')
    await flush()

    expect(wrapper.text()).toContain('Failed to save profile')
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

  it('edit: echoes the loaded team-scoped visibility instead of flipping it to org', async () => {
    const teamProfile = fullProfile({ visibility: 'team', owner_team_id: 'team-777' })
    getMock.mockResolvedValue(teamProfile)
    putMock.mockResolvedValue(teamProfile)
    const wrapper = mountForm()
    await flush()

    await wrapper.find('[data-testid="envprofile-form-name"]').setValue('Renamed')
    await wrapper.find('form').trigger('submit')
    await flush()

    const [, payload] = putMock.mock.calls[0]
    // The form does not own visibility: it must echo the loaded value so a
    // team-scoped profile (owner_team_id preserved by the backend's
    // exclude_unset update) is never silently made org-visible.
    expect(payload.visibility).toBe('team')
    expect(payload.owner_team_id).toBeUndefined()
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

describe('EnvironmentProfileForm — BYO image path and digest pinning (FAR-1779)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    routeState.params = {}
  })

  async function mountWithProvider(provider: string) {
    const wrapper = mountForm()
    await flush()
    const vm = wrapper.vm as unknown as { form: { provider_type: string } }
    vm.form.provider_type = provider
    await nextTick()
    return wrapper
  }

  it('renders persistent BYO helper copy truthful for run dispatch and Test Connection', async () => {
    const wrapper = await mountWithProvider('runner_docker')
    const help = wrapper.find('#envprofile-form-image-help')
    expect(help.exists()).toBe(true)
    expect(help.text()).toContain('Bring your own agent image')
    expect(help.text()).toContain('agent commands')
    // FAR-1779 F2 (corrected): real pipeline runs take the E2B template from
    // the sandbox_agent node, but the Runners-page Test Connection provisions
    // from this profile's image reference — the copy must state both.
    expect(help.text()).toContain('Test Connection')
    expect(help.text()).toContain('provisions a sandbox from this image reference')
    expect(help.text()).toContain('real pipeline runs take the E2B template from each sandbox_agent node')
    expect(help.text()).not.toContain('not on this profile')
  })

  it('acknowledges a digest-pinned ref and shows no mutable-tag warning', async () => {
    const wrapper = await mountWithProvider('runner_docker')
    await wrapper.find('[data-testid="envprofile-form-image"]').setValue(`ghcr.io/acme/agent@sha256:${'a'.repeat(64)}`)
    await nextTick()

    const pinned = wrapper.find('[data-testid="envprofile-form-image-pinned"]')
    expect(pinned.exists()).toBe(true)
    expect(pinned.attributes('role')).toBe('status')
    expect(pinned.text()).toContain('Pinned by digest')
    expect(wrapper.find('[data-testid="envprofile-form-image-tag-warning"]').exists()).toBe(false)
  })

  it('acknowledges a digest-pinned ref on the dispatchable runner_docker provider', async () => {
    // FAR-1779 F2: runner_docker is the provider that actually dispatches a
    // container image (local_docker is refused at dispatch), so the digest
    // classification must apply to it.
    const wrapper = await mountWithProvider('runner_docker')
    await wrapper.find('[data-testid="envprofile-form-image"]').setValue(`modulo-runner:opencode@sha256:${'c'.repeat(64)}`)
    await nextTick()

    expect(wrapper.find('[data-testid="envprofile-form-image-pinned"]').exists()).toBe(true)
  })

  it.each([
    ['uppercase digest hex', `ghcr.io/acme/agent@sha256:${'A'.repeat(64)}`],
    ['uppercase repository', `GHCR.io/acme/agent@sha256:${'a'.repeat(64)}`],
    ['uppercase namespace segment', `ghcr.io/ACME/agent@sha256:${'a'.repeat(64)}`],
    ['embedded space', `ghcr.io/acme/agent @sha256:${'a'.repeat(64)}`],
    // Docker's separator is a single `.`/`_`, a double `__`, or hyphens — not
    // an arbitrary one-or-two of [._-]. These are NOT valid references and
    // must never render the green immutability assurance.
    ['double-dot repository', `ghcr.io/acme/my..repo@sha256:${'a'.repeat(64)}`],
    ['dot-underscore repository', `ghcr.io/acme/my._repo@sha256:${'a'.repeat(64)}`],
    ['underscore-dot repository', `ghcr.io/acme/my_.repo@sha256:${'a'.repeat(64)}`],
    ['underscore-hyphen repository', `ghcr.io/acme/my-_repo@sha256:${'a'.repeat(64)}`],
    ['dot-hyphen repository', `ghcr.io/acme/my.-repo@sha256:${'a'.repeat(64)}`],
  ])('reports a ref with a %s as invalid, never pinned or mutable', async (_label, ref) => {
    // FAR-1779 F1/F3: Docker/containerd reject these, so classifying them as
    // pinned would assert immutability the runtime would refuse to honour — and
    // calling them a mutable tag would advise "pin it by digest", which is
    // exactly what the operator already tried. They get the invalid state and
    // its accurate copy.
    const wrapper = await mountWithProvider('runner_docker')
    await wrapper.find('[data-testid="envprofile-form-image"]').setValue(ref)
    await nextTick()

    expect(wrapper.find('[data-testid="envprofile-form-image-pinned"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="envprofile-form-image-tag-warning"]').exists()).toBe(false)
    const invalid = wrapper.find('[data-testid="envprofile-form-image-invalid"]')
    expect(invalid.exists()).toBe(true)
    expect(invalid.attributes('role')).toBe('status')
    expect(invalid.text()).toContain('not a valid image reference')
  })

  it.each([
    ['double-hyphen repository', `ghcr.io/acme/my--repo@sha256:${'a'.repeat(64)}`],
    ['double-underscore repository', `ghcr.io/acme/my__repo@sha256:${'a'.repeat(64)}`],
    ['triple-hyphen repository', `ghcr.io/acme/my---repo@sha256:${'a'.repeat(64)}`],
    ['registry host containing a double hyphen', `my--registry.io/acme/agent@sha256:${'a'.repeat(64)}`],
    ['registry host containing a triple hyphen', `my---registry.io/acme/agent@sha256:${'a'.repeat(64)}`],
  ])('acknowledges a valid ref with a %s as pinned', async (_label, ref) => {
    // FAR-1779 F3 (corrected): Docker's separator is a single `.`/`_`, a double
    // `__`, or a run of one-or-more `-`, so these are valid names and must not
    // be mislabelled invalid (or mutable).
    const wrapper = await mountWithProvider('runner_docker')
    await wrapper.find('[data-testid="envprofile-form-image"]').setValue(ref)
    await nextTick()

    expect(wrapper.find('[data-testid="envprofile-form-image-pinned"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="envprofile-form-image-invalid"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="envprofile-form-image-tag-warning"]').exists()).toBe(false)
  })

  it('reports the release placeholder digest as unresolvable, never pinned', async () => {
    // FAR-1779 C1: the release placeholder is syntactically a valid digest but
    // no registry image exists behind it, so the green "pinned / immutable"
    // assurance would be a lie. It gets its own unresolvable state.
    const wrapper = await mountWithProvider('runner_docker')
    await wrapper.find('[data-testid="envprofile-form-image"]').setValue(`modulo-runner:opencode@sha256:${'0'.repeat(64)}`)
    await nextTick()

    expect(wrapper.find('[data-testid="envprofile-form-image-pinned"]').exists()).toBe(false)
    const placeholder = wrapper.find('[data-testid="envprofile-form-image-placeholder"]')
    expect(placeholder.exists()).toBe(true)
    expect(placeholder.attributes('role')).toBe('status')
    expect(placeholder.text()).toContain('cannot be pulled')
  })

  it('does not digest-classify a local_docker profile because dispatch refuses the provider', async () => {
    // FAR-1779 F2: local_docker cannot dispatch a container image
    // (SandboxDispatchUnboundError), so it is NOT in the pinnable set.
    const wrapper = await mountWithProvider('local_docker')
    await wrapper.find('[data-testid="envprofile-form-image"]').setValue(`ghcr.io/acme/agent@sha256:${'a'.repeat(64)}`)
    await nextTick()

    expect(wrapper.find('[data-testid="envprofile-form-image-pinned"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="envprofile-form-image-tag-warning"]').exists()).toBe(false)
  })

  it('warns without blocking when a mutable tag is used, and still saves', async () => {
    postMock.mockResolvedValue(fullProfile({ name: 'byo' }))
    const wrapper = await mountWithProvider('runner_docker')

    await wrapper.find('[data-testid="envprofile-form-name"]').setValue('byo')
    await wrapper.find('[data-testid="envprofile-form-image"]').setValue('ghcr.io/acme/agent:latest')
    await nextTick()

    const warning = wrapper.find('[data-testid="envprofile-form-image-tag-warning"]')
    expect(warning.exists()).toBe(true)
    expect(warning.attributes('role')).toBe('status')
    expect(warning.text()).toContain('pin the image by digest')
    expect(wrapper.find('[data-testid="envprofile-form-image-pinned"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="envprofile-form-image-invalid"]').exists()).toBe(false)

    await wrapper.find('form').trigger('submit')
    await flush()

    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock.mock.calls[0][1].image_ref).toBe('ghcr.io/acme/agent:latest')
  })

  it('warns on an implicit latest ref with no tag at all', async () => {
    const wrapper = await mountWithProvider('kubernetes')
    await wrapper.find('[data-testid="envprofile-form-image"]').setValue('ghcr.io/acme/agent')
    await nextTick()

    expect(wrapper.find('[data-testid="envprofile-form-image-tag-warning"]').exists()).toBe(true)
  })

  it('does not digest-warn an E2B template ID because image_ref is not a container image', async () => {
    const wrapper = await mountWithProvider('e2b')
    await wrapper.find('[data-testid="envprofile-form-image"]').setValue('modulo-opencode')
    await nextTick()

    expect(wrapper.find('[data-testid="envprofile-form-image-tag-warning"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="envprofile-form-image-pinned"]').exists()).toBe(false)
  })

  it('references the dynamic state region only while it is rendered', async () => {
    const wrapper = await mountWithProvider('runner_docker')
    const input = wrapper.find('[data-testid="envprofile-form-image"]')
    expect(input.attributes('aria-describedby')).toBe('envprofile-form-image-help')

    await input.setValue(`ghcr.io/acme/agent@sha256:${'b'.repeat(64)}`)
    await nextTick()

    const describedBy = input.attributes('aria-describedby') ?? ''
    expect(describedBy).toContain('envprofile-form-image-help')
    expect(describedBy).toContain('envprofile-form-image-state')
  })
})
