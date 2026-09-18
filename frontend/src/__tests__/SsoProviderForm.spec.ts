import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import SsoProviderForm from '../components/SsoProviderForm.vue'
import type { SsoFormState, SsoPresetInfo } from '../components/SsoProviderForm.vue'

const SelectStub = {
  name: 'Select',
  props: ['modelValue', 'options', 'optionLabel', 'optionValue', 'placeholder', 'ariaLabel'],
  emits: ['update:modelValue'],
  template: `
    <select data-testid="mock-select" :aria-label="ariaLabel || 'select'" @change="$emit('update:modelValue', $event.target.value)">
      <option v-for="o in options" :key="o.value ?? o" :value="o.value ?? o">{{ o.label ?? o }}</option>
    </select>`,
}

const PRESETS: SsoPresetInfo[] = [
  { id: 'custom', label: 'Custom', requires_tenant: false, tenant_label: '' },
  { id: 'google', label: 'Google', requires_tenant: false, tenant_label: '' },
]

function makeData(overrides: Partial<SsoFormState> = {}): SsoFormState {
  return {
    provider_type: 'oidc',
    name: 'Test Provider',
    client_id: 'client-123',
    client_secret: '',
    discovery_url: 'https://idp.example.com/.well-known',
    metadata_url: '',
    metadata_xml: '',
    entity_id: '',
    scopes: 'openid profile email',
    auto_provision: true,
    default_role: 'runner',
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
      submitLabel: 'Create',
      savingLabel: 'Creating...',
      error: null,
      presets: PRESETS,
      ...overrides,
    },
    global: { stubs: { Select: SelectStub } },
  })
}

describe('SsoProviderForm — domain input commit on blur (FAR-974 #4)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('commits a typed domain when the input loses focus (blur)', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: [] })
    const wrapper = mountForm(data, { submitLabel: 'Save' })
    await nextTick()

    // Switch to domains mode
    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    // Type a domain but do NOT press Enter
    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('example.com')
    await nextTick()

    // The domain should NOT be in allowed_domains yet (not committed)
    const emittedBeforeBlur = wrapper.emitted('update:data')
    const lastEmitBeforeBlur = emittedBeforeBlur?.at(-1)?.[0] as SsoFormState | undefined
    // Either no emit since mode change, or the last emit still has empty domains
    if (lastEmitBeforeBlur) {
      expect(lastEmitBeforeBlur.allowed_domains).toEqual([])
    }

    // Trigger blur — this should commit the pending domain
    await input.trigger('blur')
    await nextTick()

    // Now the domain should be committed
    const emittedAfterBlur = wrapper.emitted('update:data')!
    const lastEmit = emittedAfterBlur[emittedAfterBlur.length - 1][0] as SsoFormState
    expect(lastEmit.allowed_domains).toEqual(['example.com'])
  })

  it('commits pending domain on submit even if blur was not triggered', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: [] })
    const wrapper = mountForm(data, { submitLabel: 'Save' })
    await nextTick()

    // Switch to domains mode
    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    // Type a domain
    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('corp.example.com')
    await nextTick()

    // Click Save directly — no blur, no Enter
    await wrapper.findAll('button').find((b) => b.text() === 'Save')!.trigger('click')
    await nextTick()

    // The submit handler calls commitPendingDomain before emitting 'submit',
    // so the last update:data should carry the domain
    const emitted = wrapper.emitted('update:data')!
    const lastEmit = emitted[emitted.length - 1][0] as SsoFormState
    expect(lastEmit.allowed_domains).toContain('corp.example.com')
    // The submit event should have been emitted
    expect(wrapper.emitted('submit')).toHaveLength(1)
  })

  it('does not commit an invalid domain on blur — shows validation error', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: [] })
    const wrapper = mountForm(data, { submitLabel: 'Save' })
    await nextTick()

    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('nodot')
    await nextTick()

    await input.trigger('blur')
    await nextTick()

    // Should show validation error, not add the domain
    expect(wrapper.find('[data-testid="sso-domain-error"]').exists()).toBe(true)
    const emitted = wrapper.emitted('update:data')!
    const lastEmit = emitted[emitted.length - 1][0] as SsoFormState
    expect(lastEmit.allowed_domains).toEqual([])
  })

  it('commits pending domain before mode switch away from domains', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: [] })
    const wrapper = mountForm(data, { submitLabel: 'Save' })
    await nextTick()

    // Switch to domains mode
    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    // Type a domain
    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('switched.example.com')
    await nextTick()

    // Switch to invitation mode — this should commit the pending domain first
    await wrapper.find('[data-testid="sso-mode-invitation"]').setValue(true)
    await nextTick()

    // Check that the domain was committed before the mode change cleared domains
    const emitted = wrapper.emitted('update:data')!
    const allDomains = emitted
      .map((e) => (e[0] as SsoFormState).allowed_domains)
      .flat()
    expect(allDomains).toContain('switched.example.com')
  })
})

describe('SsoProviderForm — domain paste support (FAR-974 #3)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('splits a comma-separated paste into individual domains', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: [] })
    const wrapper = mountForm(data)
    await nextTick()

    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.trigger('paste', {
      clipboardData: { getData: () => 'example.com, mail.example.com' },
    } as unknown as ClipboardEvent)
    await nextTick()

    const emitted = wrapper.emitted('update:data')!
    const lastEmit = emitted[emitted.length - 1][0] as SsoFormState
    expect(lastEmit.allowed_domains).toEqual(['example.com', 'mail.example.com'])
  })

  it('splits a space-separated paste into individual domains', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: [] })
    const wrapper = mountForm(data)
    await nextTick()

    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.trigger('paste', {
      clipboardData: { getData: () => 'foo.com bar.org baz.net' },
    } as unknown as ClipboardEvent)
    await nextTick()

    const emitted = wrapper.emitted('update:data')!
    const lastEmit = emitted[emitted.length - 1][0] as SsoFormState
    expect(lastEmit.allowed_domains).toEqual(['foo.com', 'bar.org', 'baz.net'])
  })

  it('the domain helper text mentions paste support and case-insensitive matching', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: [] })
    const wrapper = mountForm(data)
    await nextTick()

    // Switch to domains mode to make the helper text visible
    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    // The help text locale key should describe paste and case behavior
    const text = wrapper.text()
    expect(text).toMatch(/paste/i)
    expect(text).toMatch(/case/i)
    expect(text).toMatch(/subdomain/i)
  })
})

describe('SsoProviderForm — client secret placeholder and hint (FAR-974 #5)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows masked placeholder dots in edit mode', () => {
    const wrapper = mountForm(makeData(), { isEdit: true })
    const secretInput = wrapper.find('#ssoproviderform-field-7')
    expect(secretInput.attributes('placeholder')).toBe('\u2022\u2022\u2022\u2022\u2022\u2022')
  })

  it('shows "Enter client secret" placeholder in create mode', () => {
    const wrapper = mountForm(makeData(), { isEdit: false })
    const secretInput = wrapper.find('#ssoproviderform-field-7')
    expect(secretInput.attributes('placeholder')).toBe('Enter client secret')
  })

  it('shows the leave-blank hint only in edit mode', () => {
    const editWrapper = mountForm(makeData(), { isEdit: true })
    expect(editWrapper.text()).toContain('Leave blank to keep existing')

    const createWrapper = mountForm(makeData(), { isEdit: false })
    expect(createWrapper.text()).not.toContain('Leave blank to keep existing')
  })
})

describe('SsoProviderForm — domain input keyboard and validation (FAR-974 #3/#4)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('adds a domain on Enter keypress', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: [] })
    const wrapper = mountForm(data)
    await nextTick()

    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('enter-test.com')
    await input.trigger('keydown.enter')
    await nextTick()

    const emitted = wrapper.emitted('update:data')!
    const lastEmit = emitted[emitted.length - 1][0] as SsoFormState
    expect(lastEmit.allowed_domains).toEqual(['enter-test.com'])
  })

  it('rejects a domain without a dot', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: [] })
    const wrapper = mountForm(data)
    await nextTick()

    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('nodot')
    await input.trigger('keydown.enter')
    await nextTick()

    expect(wrapper.find('[data-testid="sso-domain-error"]').exists()).toBe(true)
  })

  it('rejects a domain with special characters', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: [] })
    const wrapper = mountForm(data)
    await nextTick()

    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('bad@domain.com')
    await input.trigger('keydown.enter')
    await nextTick()

    expect(wrapper.find('[data-testid="sso-domain-error"]').exists()).toBe(true)
  })

  it('normalises domains to lowercase', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: [] })
    const wrapper = mountForm(data)
    await nextTick()

    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    const input = wrapper.find('[data-testid="sso-domain-input"]')
    await input.setValue('EXAMPLE.COM')
    await input.trigger('keydown.enter')
    await nextTick()

    const emitted = wrapper.emitted('update:data')!
    const lastEmit = emitted[emitted.length - 1][0] as SsoFormState
    expect(lastEmit.allowed_domains).toEqual(['example.com'])
  })

  it('removes a domain tag via the remove button', async () => {
    const data = makeData({ auto_provision: true, allowed_domains: ['example.com', 'corp.com'] })
    const wrapper = mountForm(data)
    await nextTick()

    await wrapper.find('[data-testid="sso-mode-domains"]').setValue(true)
    await nextTick()

    // Click the remove button for the first domain
    await wrapper.find('[data-testid="sso-domain-remove-0"]').trigger('click')
    await nextTick()

    const emitted = wrapper.emitted('update:data')!
    const lastEmit = emitted[emitted.length - 1][0] as SsoFormState
    expect(lastEmit.allowed_domains).toEqual(['corp.com'])
  })
})
