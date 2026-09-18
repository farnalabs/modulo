import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, nextTick, reactive } from 'vue'

const SelectStub = {
  name: 'Select',
  props: ['modelValue', 'options', 'optionLabel', 'optionValue', 'placeholder', 'ariaLabel'],
  emits: ['update:modelValue'],
  template: `
    <select data-testid="mock-select" :aria-label="ariaLabel || 'select'" @change="$emit('update:modelValue', $event.target.value)">
      <option v-for="o in options" :key="o.value ?? o" :value="o.value ?? o">{{ o.label ?? o }}</option>
    </select>`,
}

import SsoProviderForm from '../../components/SsoProviderForm.vue'

interface SsoFormState {
  provider_type: string
  name: string
  client_id: string
  client_secret: string
  discovery_url: string
  metadata_url: string
  metadata_xml: string
  entity_id: string
  scopes: string
  auto_provision: boolean
  default_role: string
  preset: string
  tenant_domain: string
  allowed_domains: string[]
}

function makeData(overrides: Partial<SsoFormState> = {}): SsoFormState {
  return {
    provider_type: 'oidc',
    name: 'Google Workspace',
    client_id: 'client-123',
    client_secret: '',
    discovery_url: 'https://accounts.google.com/.well-known/openid-configuration',
    metadata_url: '',
    metadata_xml: '',
    entity_id: '',
    scopes: 'openid profile email',
    auto_provision: false,
    default_role: 'operator',
    preset: 'custom',
    tenant_domain: '',
    allowed_domains: [],
    ...overrides,
  }
}

function mountForm(data: SsoFormState = makeData(), overrides: Record<string, unknown> = {}) {
  return mount(SsoProviderForm, {
    props: {
      data,
      saving: false,
      submitLabel: 'Create Provider',
      savingLabel: 'Creating...',
      error: null,
      presets: [],
      ...overrides,
    },
    global: { stubs: { Select: SelectStub } },
  })
}



describe('SsoProviderForm', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the provider-type switcher with oidc active', () => {
    const wrapper = mountForm()
    const buttons = wrapper.findAll('button').filter(b => b.text().includes('OIDC') || b.text().includes('SAML'))
    expect(buttons).toHaveLength(2)
    expect(buttons[0].text()).toContain('OpenID Connect')
    expect(buttons[1].text()).toContain('SAML')
    expect(buttons[0].classes()).toContain('border-primary')
    expect(buttons[1].classes()).not.toContain('border-primary')
  })

  it('renders OIDC fields for an oidc provider and emits typed values', async () => {
    const wrapper = mountForm()
    expect(wrapper.text()).toContain('Client ID')
    expect(wrapper.text()).toContain('Client Secret')
    expect(wrapper.text()).toContain('Discovery URL')
    expect(wrapper.text()).toContain('Scopes')
    const clientId = wrapper.find('#ssoproviderform-field-8')
    expect((clientId.element as HTMLInputElement).value).toBe('client-123')
    await clientId.setValue('new-client')
    const emitted = wrapper.emitted('update:data')!
    expect(emitted).toHaveLength(1)
    expect((emitted[0][0] as SsoFormState).client_id).toBe('new-client')
  })

  it('renders SAML fields for a saml provider and emits typed values', async () => {
    const wrapper = mountForm(makeData({ provider_type: 'saml', metadata_url: 'https://idp.example.com/metadata.xml' }))
    expect(wrapper.text()).toContain('Metadata URL')
    expect(wrapper.text()).toContain('Metadata XML')
    expect(wrapper.text()).toContain('Entity ID')
    expect(wrapper.find('#ssoproviderform-field-8').exists()).toBe(false)
    await wrapper.find('#ssoproviderform-field-4').setValue('https://idp.example.com/federationmetadata.xml')
    expect((wrapper.emitted('update:data')![0][0] as SsoFormState).metadata_url).toBe('https://idp.example.com/federationmetadata.xml')
  })

  it('switches provider type to saml and clears oidc-only fields in the payload', async () => {
    const wrapper = mountForm()
    const buttons = wrapper.findAll('button').filter(b => b.text().includes('OIDC') || b.text().includes('SAML'))
    await buttons[1].trigger('click')
    const payload = wrapper.emitted('update:data')![0][0] as SsoFormState
    expect(payload.provider_type).toBe('saml')
    expect(payload.client_id).toBe('')
    expect(payload.client_secret).toBe('')
    expect(payload.discovery_url).toBe('')
    expect(payload.scopes).toBe('')
    expect(payload.name).toBe('Google Workspace')
  })

  it('switches provider type to oidc and clears saml-only fields in the payload', async () => {
    const wrapper = mountForm(makeData({ provider_type: 'saml', metadata_url: 'u', metadata_xml: 'x', entity_id: 'e' }))
    const buttons = wrapper.findAll('button').filter(b => b.text().includes('OIDC') || b.text().includes('SAML'))
    await buttons[0].trigger('click')
    const payload = wrapper.emitted('update:data')![0][0] as SsoFormState
    expect(payload.provider_type).toBe('oidc')
    expect(payload.metadata_url).toBe('')
    expect(payload.metadata_xml).toBe('')
    expect(payload.entity_id).toBe('')
  })

  it('emits the typed name', async () => {
    const wrapper = mountForm()
    await wrapper.find('#ssoproviderform-field-9').setValue('Okta')
    expect((wrapper.emitted('update:data')![0][0] as SsoFormState).name).toBe('Okta')
  })

  it('selects invitation mode and emits auto_provision=false with empty domains', async () => {
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: ['example.com'] }))
    const invitationRadio = wrapper.find('[data-testid="sso-mode-invitation"]')
    expect(invitationRadio.exists()).toBe(true)
    await invitationRadio.setValue(true)
    const payload = wrapper.emitted('update:data')![0][0] as SsoFormState
    expect(payload.auto_provision).toBe(false)
    expect(payload.allowed_domains).toEqual([])
  })

  it('selects domains mode and emits auto_provision=true', async () => {
    const wrapper = mountForm(makeData({ auto_provision: false }))
    const domainsRadio = wrapper.find('[data-testid="sso-mode-domains"]')
    expect(domainsRadio.exists()).toBe(true)
    await domainsRadio.setValue(true)
    const payload = wrapper.emitted('update:data')![0][0] as SsoFormState
    expect(payload.auto_provision).toBe(true)
  })

  it('latches domains mode with an empty allowlist so the first domain can be added', async () => {
    // Regression (review finding 1): the payload for domains-mode-with-an-empty
    // list is identical to unrestricted (auto_provision=true, no domains), so
    // re-deriving the mode from the payload made the radio snap back and the
    // domain input never appeared. The explicit chosen-mode state keeps the
    // input visible once the user has chosen it. A harness applies the emits
    // back into the data prop exactly like SettingsSsoView's Object.assign.
    const Harness = defineComponent({
      components: { SsoProviderForm },
      setup() {
        const data = reactive(makeData({ auto_provision: false, allowed_domains: [] }))
        return {
          data,
          onUpdate: (value: SsoFormState) => Object.assign(data, value),
        }
      },
      template: `
        <SsoProviderForm :data="data" :saving="false" submit-label="Create Provider"
          saving-label="Creating..." :error="null" :presets="[]" @update:data="onUpdate" />`,
    })
    const wrapper = mount(Harness, { global: { stubs: { Select: SelectStub } } })

    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    const input = wrapper.find('[data-testid="sso-domain-input"]')
    expect(input.exists()).toBe(true)

    await input.setValue('first.com')
    await input.trigger('keydown.enter')
    await nextTick()

    const last = wrapper.findComponent(SsoProviderForm).emitted('update:data')!.at(-1)![0] as SsoFormState
    expect(last.auto_provision).toBe(true)
    expect(last.allowed_domains).toEqual(['first.com'])
  })

  it('hides unrestricted mode when flag is off', () => {
    const wrapper = mountForm(makeData(), { unrestrictedProvisioningAvailable: false })
    expect(wrapper.find('[data-testid="sso-mode-unrestricted"]').exists()).toBe(false)
  })

  it('shows unrestricted mode when flag is on', () => {
    const wrapper = mountForm(makeData(), { unrestrictedProvisioningAvailable: true })
    expect(wrapper.find('[data-testid="sso-mode-unrestricted"]').exists()).toBe(true)
  })

  it('selects unrestricted mode and emits auto_provision=true with empty domains', async () => {
    const wrapper = mountForm(makeData({ auto_provision: false }), { unrestrictedProvisioningAvailable: true })
    const unrestrictedRadio = wrapper.find('[data-testid="sso-mode-unrestricted"]')
    await unrestrictedRadio.setValue(true)
    const payload = wrapper.emitted('update:data')![0][0] as SsoFormState
    expect(payload.auto_provision).toBe(true)
    expect(payload.allowed_domains).toEqual([])
  })

  it('shows unrestricted danger warning when unrestricted mode is selected', async () => {
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: [] }), { unrestrictedProvisioningAvailable: true })
    expect(wrapper.find('[data-testid="sso-unrestricted-warning"]').exists()).toBe(true)
  })

  it('surfaces a locked notice when the current unrestricted mode is plan-gated off', () => {
    // Review finding 2: a legacy provider that is already unrestricted shows no
    // selected radio when the option is hidden, so the state (and the fact that
    // saving re-commits it) must be made explicit.
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: [] }), {
      unrestrictedProvisioningAvailable: false,
    })
    expect(wrapper.find('[data-testid="sso-mode-unrestricted"]').exists()).toBe(false)
    const notice = wrapper.find('[data-testid="sso-unrestricted-locked-notice"]')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('still auto-provisions anyone who authenticates')
  })

  it('hides the locked notice when the unrestricted option is available', () => {
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: [] }), {
      unrestrictedProvisioningAvailable: true,
    })
    expect(wrapper.find('[data-testid="sso-unrestricted-locked-notice"]').exists()).toBe(false)
  })

  it('clears the locked notice once another provisioning mode is chosen', async () => {
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: [] }), {
      unrestrictedProvisioningAvailable: false,
    })
    expect(wrapper.find('[data-testid="sso-unrestricted-locked-notice"]').exists()).toBe(true)
    await wrapper.find('[data-testid="sso-mode-invitation"]').setValue(true)
    expect(wrapper.find('[data-testid="sso-unrestricted-locked-notice"]').exists()).toBe(false)
  })

  it('emits the selected default role', async () => {
    const wrapper = mountForm()
    await wrapper.find('[data-testid="mock-select"]').setValue('runner')
    const payload = wrapper.emitted('update:data')![0][0] as SsoFormState
    expect(payload.default_role).toBe('runner')
  })

  it('disables submit while the name is blank or a save is running', async () => {
    const wrapper = mountForm(makeData({ name: '  ' }))
    const submit = wrapper.findAll('button').find(b => b.text() === 'Create Provider')
    expect(submit!.attributes('disabled')).toBeDefined()
    const wrapperSaving = mountForm(makeData(), { saving: true, savingLabel: 'Creating...' })
    const savingBtn = wrapperSaving.findAll('button').find(b => b.text() === 'Creating...')
    expect(savingBtn).toBeDefined()
    expect(savingBtn!.attributes('disabled')).toBeDefined()
  })

  it('emits submit and cancel from the footer buttons', async () => {
    const wrapper = mountForm()
    const submit = wrapper.findAll('button').find(b => b.text() === 'Create Provider')
    await submit!.trigger('click')
    expect(wrapper.emitted('submit')).toHaveLength(1)
    const cancel = wrapper.findAll('button').find(b => b.text() === 'Cancel')
    await cancel!.trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)
  })

  it('shows the error prop inline', () => {
    const wrapper = mountForm(makeData(), { error: 'Provider validation failed' })
    expect(wrapper.text()).toContain('Provider validation failed')
  })

  it('keeps the secret field as a password input', () => {
    const wrapper = mountForm()
    expect(wrapper.find('#ssoproviderform-field-7').attributes('type')).toBe('password')
  })

  it('emits the metadata XML textarea content for saml providers', async () => {
    const wrapper = mountForm(makeData({ provider_type: 'saml' }))
    await wrapper.find('#ssoproviderform-field-3').setValue('<EntityDescriptor/>')
    await nextTick()
    expect((wrapper.emitted('update:data')![0][0] as SsoFormState).metadata_xml).toBe('<EntityDescriptor/>')
  })

  it('shows the domain input only in domains mode', () => {
    // Data with auto_provision=true and at least one domain -> deriveMode = 'domains'
    const wrapperDomains = mountForm(makeData({ auto_provision: true, allowed_domains: ['example.com'] }))
    expect(wrapperDomains.find('[data-testid="sso-domain-input"]').exists()).toBe(true)

    // Data with auto_provision=false -> deriveMode = 'invitation' -> no domain input
    const wrapperInvite = mountForm(makeData({ auto_provision: false }))
    expect(wrapperInvite.find('[data-testid="sso-domain-input"]').exists()).toBe(false)
  })

  it('adds a domain on Enter and emits the updated list', async () => {
    // Start already in domains mode so the input is visible
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: [] }))

    const input = wrapper.find('[data-testid="sso-domain-input"]')
    // Domain input should not be visible for empty domains + auto_provision
    // (deriveMode returns 'unrestricted' in that case). Use existing domains to stay in mode 2.
    expect(input.exists()).toBe(false)

    // Use data that keeps us in domains mode
    const wrapper2 = mountForm(makeData({ auto_provision: true, allowed_domains: ['existing.com'] }))
    const input2 = wrapper2.find('[data-testid="sso-domain-input"]')
    await input2.setValue('example.com')
    await input2.trigger('keydown.enter')
    await nextTick()

    const payload = wrapper2.emitted('update:data')![0][0] as SsoFormState
    expect(payload.allowed_domains).toEqual(['existing.com', 'example.com'])
  })

  it('rejects domains containing @', async () => {
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: ['example.com'] }))
    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('user@example.com')
    await input.trigger('keydown.enter')
    await nextTick()

    const error = wrapper.find('[data-testid="sso-domain-error"]')
    expect(error.exists()).toBe(true)
    // Review finding 3: the message must come from the locale, not hardcoded
    // English in the component.
    expect(error.text()).toContain('no @, URL scheme, path, or wildcard')
    // No domain-add emit
    const allEmits = wrapper.emitted('update:data')
    expect(allEmits ?? []).toHaveLength(0)
  })

  it('rejects domains containing a scheme', async () => {
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: ['example.com'] }))
    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('https://example.com')
    await input.trigger('keydown.enter')
    await nextTick()

    expect(wrapper.find('[data-testid="sso-domain-error"]').exists()).toBe(true)
  })

  it('rejects domains containing a path', async () => {
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: ['example.com'] }))
    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('example.com/path')
    await input.trigger('keydown.enter')
    await nextTick()

    expect(wrapper.find('[data-testid="sso-domain-error"]').exists()).toBe(true)
  })

  it('rejects domains without a dot', async () => {
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: ['example.com'] }))
    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('localhost')
    await input.trigger('keydown.enter')
    await nextTick()

    const error = wrapper.find('[data-testid="sso-domain-error"]')
    expect(error.exists()).toBe(true)
    // Review finding 3: the no-dot message is localised too.
    expect(error.text()).toContain('must contain a dot')
  })

  it('deduplicates domains', async () => {
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: ['example.com'] }))
    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('Example.COM')
    await input.trigger('keydown.enter')
    await nextTick()

    // Should not add duplicate (normalised) — no new emit
    expect(wrapper.emitted('update:data') ?? []).toHaveLength(0)
  })

  it('removes a domain via the remove button', async () => {
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: ['example.com', 'test.org'] }))
    await wrapper.find('[data-testid="sso-domain-remove-0"]').trigger('click')
    await nextTick()

    const payload = wrapper.emitted('update:data')![0][0] as SsoFormState
    expect(payload.allowed_domains).toEqual(['test.org'])
  })

  // ── Fix #4 regression: pending domain committed on blur ──────────
  it('commits a typed domain on blur without requiring Enter (FAR-974 #4 regression)', async () => {
    const Harness = defineComponent({
      components: { SsoProviderForm },
      setup() {
        // Start in invitation mode; user selects domains mode
        const data = reactive(makeData({ auto_provision: false, allowed_domains: [] }))
        return {
          data,
          onUpdate: (value: SsoFormState) => Object.assign(data, value),
        }
      },
      template: `
        <SsoProviderForm :data="data" :saving="false" submit-label="Save"
          saving-label="Saving..." :error="null" :presets="[]" @update:data="onUpdate" />`,
    })
    const wrapper = mount(Harness, { global: { stubs: { Select: SelectStub } } })

    // Switch to domains mode so the domain input appears
    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    const input = wrapper.find('[data-testid="sso-domain-input"]')
    expect(input.exists()).toBe(true)
    await input.setValue('modulo.run')
    // Trigger blur — should commit the domain without Enter
    await input.trigger('blur')
    await nextTick()

    const last = wrapper.findComponent(SsoProviderForm).emitted('update:data')!.at(-1)![0] as SsoFormState
    expect(last.allowed_domains).toEqual(['modulo.run'])
  })

  it('does not re-emit a pending domain already in the allowlist on blur (FAR-974 #4)', async () => {
    const wrapper = mountForm(makeData({ auto_provision: true, allowed_domains: ['example.com'] }))
    const input = wrapper.find('[data-testid="sso-domain-input"]')

    // Normalises to example.com, which is already allowlisted — blur commits
    // silently (clears the input) instead of emitting a duplicate.
    await input.setValue('Example.COM')
    await input.trigger('blur')
    await nextTick()

    expect(wrapper.emitted('update:data') ?? []).toHaveLength(0)
  })

  it('commits pending domain before submit (FAR-974 #4 regression)', async () => {
    const Harness = defineComponent({
      components: { SsoProviderForm },
      setup() {
        const data = reactive(makeData({ auto_provision: true, allowed_domains: [] }))
        return {
          data,
          onUpdate: (value: SsoFormState) => Object.assign(data, value),
        }
      },
      template: `
        <SsoProviderForm :data="data" :saving="false" submit-label="Save"
          saving-label="Saving..." :error="null" :presets="[]" @update:data="onUpdate" @submit="submitted = true" />
        <span v-if="submitted" data-testid="submitted-flag">submitted</span>`,
    })
    const wrapper = mount(Harness, { global: { stubs: { Select: SelectStub } } })

    // Switch to domains mode so the domain input appears

    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)

    await nextTick()



    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('example.com')
    // Do NOT press Enter — click Save directly
    await wrapper.findAll('button').find(b => b.text() === 'Save')!.trigger('click')
    await nextTick()

    const emits = wrapper.findComponent(SsoProviderForm).emitted('update:data')!
    const lastPayload = emits.at(-1)![0] as SsoFormState
    expect(lastPayload.allowed_domains).toEqual(['example.com'])
  })

  it('surfaces validation error for invalid pending domain at save time and does not submit', async () => {
    const Harness = defineComponent({
      components: { SsoProviderForm },
      setup() {
        const data = reactive(makeData({ auto_provision: false, allowed_domains: [] }))
        return {
          data,
          submitted: false,
          onUpdate: (value: SsoFormState) => Object.assign(data, value),
        }
      },
      template: `
        <SsoProviderForm :data="data" :saving="false" submit-label="Save"
          saving-label="Saving..." :error="null" :presets="[]" @update:data="onUpdate" @submit="submitted = true" />
        <span v-if="submitted" data-testid="submitted-flag">submitted</span>`,
    })
    const wrapper = mount(Harness, { global: { stubs: { Select: SelectStub } } })

    // Switch to domains mode
    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('not-a-domain')
    await wrapper.findAll('button').find(b => b.text() === 'Save')!.trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="submitted-flag"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="sso-domain-error"]').exists()).toBe(true)
  })

  // ── Fix #3: comma/space-separated paste (batch-add via addDomain) ──
  it('adds multiple domains from comma-separated paste (jsdom lacks ClipboardEvent)', async () => {
    // jsdom lacks ClipboardEvent; test batch-add by calling addDomain in sequence
    // which exercises the same domain-validation + emit path as handleDomainPaste
    const Harness = defineComponent({
      components: { SsoProviderForm },
      setup() {
        const data = reactive(makeData({ auto_provision: true, allowed_domains: ['existing.com'] }))
        return {
          data,
          onUpdate: (value: SsoFormState) => Object.assign(data, value),
        }
      },
      template: `
        <SsoProviderForm :data="data" :saving="false" submit-label="Save"
          saving-label="Saving..." :error="null" :presets="[]" @update:data="onUpdate" />`,
    })
    const wrapper = mount(Harness, { global: { stubs: { Select: SelectStub } } })
    const input = wrapper.find('[data-testid="sso-domain-input"]')

    // Add three domains one-by-one via Enter (same logic as paste split)
    for (const domain of ['example.com', 'mail.example.com', 'test.org']) {
      await input.setValue(domain)
      await input.trigger('keydown.enter')
      await nextTick()
    }

    const payload = wrapper.findComponent(SsoProviderForm).emitted('update:data')!.at(-1)![0] as SsoFormState
    expect(payload.allowed_domains).toEqual(['existing.com', 'example.com', 'mail.example.com', 'test.org'])
  })

  it('batch-adds comma-separated domains on a real paste event (FAR-974 #3)', async () => {
    // jsdom has no ClipboardEvent constructor, so dispatch a plain Event and
    // attach a clipboardData stub — exactly what the handler reads.
    const Harness = defineComponent({
      components: { SsoProviderForm },
      setup() {
        const data = reactive(makeData({ auto_provision: true, allowed_domains: ['existing.com'] }))
        return {
          data,
          onUpdate: (value: SsoFormState) => Object.assign(data, value),
        }
      },
      template: `
        <SsoProviderForm :data="data" :saving="false" submit-label="Save"
          saving-label="Saving..." :error="null" :presets="[]" @update:data="onUpdate" />`,
    })
    const wrapper = mount(Harness, { global: { stubs: { Select: SelectStub } } })
    const input = wrapper.find('[data-testid="sso-domain-input"]')

    const pasteEvent = new Event('paste', { bubbles: true, cancelable: true })
    Object.defineProperty(pasteEvent, 'clipboardData', {
      value: { getData: () => 'example.com, mail.example.com' },
    })
    input.element.dispatchEvent(pasteEvent)
    await nextTick()

    const payload = wrapper.findComponent(SsoProviderForm).emitted('update:data')!.at(-1)![0] as SsoFormState
    expect(payload.allowed_domains).toEqual(['existing.com', 'example.com', 'mail.example.com'])
    expect(wrapper.find('[data-testid="sso-domain-error"]').exists()).toBe(false)
  })

  it('surfaces an error when a pasted list contains an invalid domain (FAR-974 #3)', async () => {
    const Harness = defineComponent({
      components: { SsoProviderForm },
      setup() {
        const data = reactive(makeData({ auto_provision: true, allowed_domains: ['existing.com'] }))
        return {
          data,
          onUpdate: (value: SsoFormState) => Object.assign(data, value),
        }
      },
      template: `
        <SsoProviderForm :data="data" :saving="false" submit-label="Save"
          saving-label="Saving..." :error="null" :presets="[]" @update:data="onUpdate" />`,
    })
    const wrapper = mount(Harness, { global: { stubs: { Select: SelectStub } } })
    const input = wrapper.find('[data-testid="sso-domain-input"]')

    const pasteEvent = new Event('paste', { bubbles: true, cancelable: true })
    Object.defineProperty(pasteEvent, 'clipboardData', {
      value: { getData: () => 'example.com, not-a-domain' },
    })
    input.element.dispatchEvent(pasteEvent)
    await nextTick()

    // The valid prefix was added before the invalid entry aborted the batch.
    const payload = wrapper.findComponent(SsoProviderForm).emitted('update:data')!.at(-1)![0] as SsoFormState
    expect(payload.allowed_domains).toEqual(['existing.com', 'example.com'])
    expect(wrapper.find('[data-testid="sso-domain-error"]').exists()).toBe(true)
  })

  // ── Fix #5: secret placeholder and hint ──────────────────────────
  it('shows masked placeholder for client secret when editing (isEdit)', () => {
    const wrapper = mountForm(makeData(), { isEdit: true })
    const secretInput = wrapper.find('#ssoproviderform-field-7')
    // The placeholder should be the masked dots, not the "leave blank" text
    expect(secretInput.attributes('placeholder')).toContain('\u2022')
  })

  it('shows "leave blank" hint text below the secret field when editing', () => {
    const wrapper = mountForm(makeData(), { isEdit: true })
    expect(wrapper.text()).toContain('Leave blank to keep existing')
  })

  it('shows "enter client secret" placeholder when creating (no isEdit)', () => {
    const wrapper = mountForm(makeData(), { isEdit: false })
    const secretInput = wrapper.find('#ssoproviderform-field-7')
    // Placeholder should prompt for input, not the "leave blank" text (which is for editing)
    expect(secretInput.attributes('placeholder')).toBe('Enter client secret')
    // No hint paragraph should appear below the field
    expect(wrapper.findAll('p').filter(p => p.text().includes('Leave blank to keep existing'))).toHaveLength(0)
  })
})
