import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

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

  it('toggles auto-provision on click and keyboard, round-tripping through props', async () => {
    const wrapper = mountForm()
    const toggle = wrapper.find('[role="switch"]')
    expect(toggle.attributes('aria-checked')).toBe('false')
    await toggle.trigger('click')
    let payload = wrapper.emitted('update:data')![0][0] as SsoFormState
    expect(payload.auto_provision).toBe(true)
    // The parent would v-model the new state back into props — simulate that.
    await wrapper.setProps({ data: payload })
    await toggle.trigger('keydown.enter')
    payload = wrapper.emitted('update:data')![1][0] as SsoFormState
    expect(payload.auto_provision).toBe(false)
    await wrapper.setProps({ data: payload })
    await toggle.trigger('keydown.space')
    payload = wrapper.emitted('update:data')![2][0] as SsoFormState
    expect(payload.auto_provision).toBe(true)
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
})
