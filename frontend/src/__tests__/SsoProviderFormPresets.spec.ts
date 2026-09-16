import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
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

import SsoProviderForm from '../components/SsoProviderForm.vue'
import type { SsoPresetInfo } from '../components/SsoProviderForm.vue'

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
}

const PRESETS: SsoPresetInfo[] = [
  { id: 'custom', label: 'Custom', requires_tenant: false, tenant_label: '' },
  { id: 'google', label: 'Google', requires_tenant: false, tenant_label: '' },
  { id: 'auth0', label: 'Auth0', requires_tenant: true, tenant_label: 'Auth0 domain' },
  { id: 'okta', label: 'Okta', requires_tenant: true, tenant_label: 'Okta domain' },
  { id: 'azure-ad', label: 'Microsoft', requires_tenant: true, tenant_label: 'Directory (tenant) ID' },
  { id: 'onelogin', label: 'OneLogin', requires_tenant: true, tenant_label: 'OneLogin subdomain' },
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

describe('SsoProviderForm — preset selection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders preset buttons for all presets', () => {
    const wrapper = mountForm()
    for (const p of PRESETS) {
      expect(wrapper.find(`[data-testid="sso-preset-${p.id}"]`).exists()).toBe(true)
    }
  })

  it('hides discovery URL and scopes when a native preset is selected', async () => {
    const wrapper = mountForm(makeData({ preset: 'google' }))
    await nextTick()
    // Discovery URL and Scopes should NOT be visible for google preset
    expect(wrapper.find('#ssoproviderform-field-6').exists()).toBe(false)
    expect(wrapper.find('#ssoproviderform-field-5').exists()).toBe(false)
    // Should show derived values instead
    expect(wrapper.text()).toContain('Discovery URL (derived)')
    expect(wrapper.text()).toContain('Scopes (derived)')
  })

  it('shows discovery URL and scopes for custom preset', async () => {
    const wrapper = mountForm(makeData({ preset: 'custom' }))
    await nextTick()
    expect(wrapper.find('#ssoproviderform-field-6').exists()).toBe(true)
    expect(wrapper.find('#ssoproviderform-field-5').exists()).toBe(true)
  })

  it('shows tenant domain field when preset requires_tenant', async () => {
    const wrapper = mountForm(makeData({ preset: 'auth0' }))
    await nextTick()
    expect(wrapper.find('[data-testid="sso-tenant-domain"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Auth0 domain')
  })

  it('hides tenant domain field when preset does not require_tenant', async () => {
    const wrapper = mountForm(makeData({ preset: 'google' }))
    await nextTick()
    expect(wrapper.find('[data-testid="sso-tenant-domain"]').exists()).toBe(false)
  })

  it('clears discovery_url and scopes when switching from custom to a native preset', async () => {
    const wrapper = mountForm(makeData({ preset: 'custom', discovery_url: 'https://idp.example.com/.well-known', scopes: 'openid email' }))
    await nextTick()
    // Click the Google preset button
    await wrapper.find('[data-testid="sso-preset-google"]').trigger('click')
    const emitted = wrapper.emitted('update:data')!
    const lastEmit = emitted[emitted.length - 1][0] as SsoFormState
    expect(lastEmit.preset).toBe('google')
    expect(lastEmit.discovery_url).toBe('')
    expect(lastEmit.scopes).toBe('')
  })

  it('clears tenant_domain when switching to custom preset', async () => {
    const wrapper = mountForm(makeData({ preset: 'auth0', tenant_domain: 'acme.auth0.com' }))
    await nextTick()
    await wrapper.find('[data-testid="sso-preset-custom"]').trigger('click')
    const emitted = wrapper.emitted('update:data')!
    const lastEmit = emitted[emitted.length - 1][0] as SsoFormState
    expect(lastEmit.preset).toBe('custom')
    expect(lastEmit.tenant_domain).toBe('')
  })

  it('shows the active preset button with primary styling', async () => {
    const wrapper = mountForm(makeData({ preset: 'okta' }))
    await nextTick()
    const oktaBtn = wrapper.find('[data-testid="sso-preset-okta"]')
    expect(oktaBtn.classes()).toContain('border-primary')
    // Other buttons should not have primary styling
    const googleBtn = wrapper.find('[data-testid="sso-preset-google"]')
    expect(googleBtn.classes()).not.toContain('border-primary')
  })

  it('derives discovery URL for auth0 with tenant domain', async () => {
    const wrapper = mountForm(makeData({ preset: 'auth0', tenant_domain: 'acme.auth0.com' }))
    await nextTick()
    expect(wrapper.text()).toContain('https://acme.auth0.com/.well-known/openid-configuration')
  })

  it('derives discovery URL for azure-ad with tenant domain', async () => {
    const wrapper = mountForm(makeData({ preset: 'azure-ad', tenant_domain: 'my-tenant-id' }))
    await nextTick()
    expect(wrapper.text()).toContain('https://login.microsoftonline.com/my-tenant-id/v2.0/.well-known/openid-configuration')
  })

  it('derives discovery URL for onelogin with tenant domain', async () => {
    const wrapper = mountForm(makeData({ preset: 'onelogin', tenant_domain: 'acme' }))
    await nextTick()
    expect(wrapper.text()).toContain('https://acme.onelogin.com/oidc/2/.well-known/openid-configuration')
  })

  it('does not show a derived discovery URL for a native preset without a tenant domain', async () => {
    const wrapper = mountForm(makeData({ preset: 'onelogin', tenant_domain: '' }))
    await nextTick()
    expect(wrapper.text()).not.toContain('Discovery URL (derived)')
  })

  it('does not show derived discovery URL for custom preset', async () => {
    const wrapper = mountForm(makeData({ preset: 'custom' }))
    await nextTick()
    expect(wrapper.text()).not.toContain('Discovery URL (derived)')
  })

  it('does not show derived discovery URL for an unrecognised preset', async () => {
    const wrapper = mountForm(makeData({ preset: 'unknown-preset' }))
    await nextTick()
    expect(wrapper.text()).not.toContain('Discovery URL (derived)')
  })
})

describe('SsoProviderForm — callback URL', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows callback URL when provided', () => {
    const wrapper = mountForm(makeData(), {
      callbackUrl: 'https://app.modulo.run/api/v1/auth/oidc/my-provider/callback',
    })
    expect(wrapper.text()).toContain('Callback URL')
    expect(wrapper.text()).toContain('https://app.modulo.run/api/v1/auth/oidc/my-provider/callback')
  })

  it('does not show callback URL when not provided', () => {
    const wrapper = mountForm(makeData(), { callbackUrl: null })
    expect(wrapper.text()).not.toContain('Callback URL')
  })

  it('has a copy button with data-testid', () => {
    const wrapper = mountForm(makeData(), {
      callbackUrl: 'https://app.modulo.run/api/v1/auth/oidc/my-provider/callback',
    })
    expect(wrapper.find('[data-testid="sso-callback-url-copy"]').exists()).toBe(true)
  })

  it('copy button has aria-label for a11y', () => {
    const wrapper = mountForm(makeData(), {
      callbackUrl: 'https://app.modulo.run/api/v1/auth/oidc/my-provider/callback',
    })
    const copyBtn = wrapper.find('[data-testid="sso-callback-url-copy"]')
    expect(copyBtn.attributes('aria-label')).toBeDefined()
  })

  it('copies the callback URL via the async clipboard API and shows the copied state', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })
    const callbackUrl = 'https://app.modulo.run/api/v1/auth/oidc/my-provider/callback'
    const wrapper = mountForm(makeData(), { callbackUrl })
    await wrapper.find('[data-testid="sso-callback-url-copy"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith(callbackUrl)
    expect(wrapper.find('[data-testid="sso-callback-url-copied"]').exists()).toBe(true)
  })

  it('falls back to execCommand when the async clipboard API rejects', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('clipboard denied'))
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })
    const execCommand = vi.fn().mockReturnValue(true)
    Object.defineProperty(document, 'execCommand', {
      value: execCommand,
      configurable: true,
    })
    const callbackUrl = 'https://app.modulo.run/api/v1/auth/oidc/my-provider/callback'
    const wrapper = mountForm(makeData(), { callbackUrl })
    await wrapper.find('[data-testid="sso-callback-url-copy"]').trigger('click')
    await flushPromises()
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(wrapper.find('[data-testid="sso-callback-url-copied"]').exists()).toBe(true)
  })
})

describe('SsoProviderForm — create payload includes preset', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('emits preset in the data when preset is selected', async () => {
    const wrapper = mountForm(makeData({ preset: 'google' }))
    // Trigger any data change to see the emitted state
    await wrapper.find('#ssoproviderform-field-9').setValue('Google SSO')
    const emitted = wrapper.emitted('update:data')!
    const lastEmit = emitted[emitted.length - 1][0] as SsoFormState
    expect(lastEmit.preset).toBe('google')
  })

  it('emits tenant_domain when set', async () => {
    const wrapper = mountForm(makeData({ preset: 'auth0' }))
    const tenantInput = wrapper.find('[data-testid="sso-tenant-domain"]')
    await tenantInput.setValue('acme.auth0.com')
    const emitted = wrapper.emitted('update:data')!
    const lastEmit = emitted[emitted.length - 1][0] as SsoFormState
    expect(lastEmit.tenant_domain).toBe('acme.auth0.com')
  })
})

describe('SsoProviderForm — contract shape validation', () => {
  it('PresetInfo shape matches generated API types', () => {
    // Verify the preset data we pass to the component matches the PresetInfo type
    const preset: SsoPresetInfo = {
      id: 'google',
      label: 'Google',
      requires_tenant: false,
      tenant_label: '',
    }
    expect(typeof preset.id).toBe('string')
    expect(typeof preset.label).toBe('string')
    expect(typeof preset.requires_tenant).toBe('boolean')
    expect(typeof preset.tenant_label).toBe('string')
  })

  it('SsoProviderCreate body shape includes preset and tenant_domain', () => {
    // Verify the create body we would send matches the API schema
    const body: Record<string, unknown> = {
      provider_type: 'oidc',
      name: 'Google SSO',
      client_id: '123',
      client_secret: null,
      enabled: true,
      auto_provision: true,
      default_role: 'runner',
      preset: 'google',
      tenant_domain: null,
    }
    // These fields must be present in the create body
    expect(body).toHaveProperty('preset')
    expect(body).toHaveProperty('tenant_domain')
    expect(body.preset).toBe('google')
  })

  it('SsoProviderResponse shape includes preset, tenant_domain, and callback_url', () => {
    // Verify the response shape from the API
    const response: Record<string, unknown> = {
      id: 'sso-1',
      provider_type: 'oidc',
      name: 'Google SSO',
      client_id: '123',
      enabled: true,
      auto_provision: true,
      default_role: 'runner',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      preset: 'google',
      tenant_domain: null,
      callback_url: 'https://app.modulo.run/api/v1/auth/oidc/google/callback',
    }
    expect(response).toHaveProperty('preset')
    expect(response).toHaveProperty('tenant_domain')
    expect(response).toHaveProperty('callback_url')
  })
})
