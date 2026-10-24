import { describe, it, expect, beforeEach } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { format, parseISO } from 'date-fns'
import LicenceRenewalNotice from '../components/licence/LicenceRenewalNotice.vue'
import type { components } from '../lib/api/schema'

// The fixture is typed against the GENERATED OpenAPI schema, so a renamed or
// removed wire field fails `vue-tsc` here instead of silently rendering
// `undefined`. That is the point of this contract: the component consumes
// `licence_state` / `renewal_mode` / `days_until_expiry` / `grace_ends_at`
// exactly as the backend sends them.
type LicenseStatus = components['schemas']['LicenseStatusResponse']

const EXPIRES_AT = '2026-06-20T10:00:00Z'
const GRACE_ENDS_AT = '2026-07-04T10:00:00Z'

function status(overrides: Partial<LicenseStatus> = {}): LicenseStatus {
  return {
    has_license: true,
    tier: 'team',
    features: ['sso'],
    expires_at: EXPIRES_AT,
    org_id: 'test-org',
    renewal_mode: 'manual',
    term_months: 12,
    licence_id: 'lic-1',
    issued_at: '2025-06-20T10:00:00Z',
    days_until_expiry: 13,
    grace_ends_at: GRACE_ENDS_AT,
    licence_state: 'renewal_due',
    ...overrides,
  }
}

const dialogStub = {
  name: 'Dialog',
  props: ['visible'],
  template: '<div v-if="visible"><slot /><slot name="footer" /></div>',
}

function mountNotice(overrides: Partial<LicenseStatus> = {}) {
  return mount(LicenceRenewalNotice, {
    props: { status: status(overrides) },
    global: { plugins: [createPinia()], stubs: { Dialog: dialogStub } },
  })
}

function formatted(iso: string): string {
  const date = parseISO(iso)
  if (Number.isNaN(date.getTime())) throw new Error(`invalid date fixture: ${iso}`)
  return format(date, 'MMMM d, yyyy')
}

describe('LicenceRenewalNotice', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  // ── Renewal countdown ──────────────────────────────────────────────────

  describe('renewal_due', () => {
    it('manual mode names the expiry date and the capability that stops', () => {
      const wrapper = mountNotice({ licence_state: 'renewal_due', renewal_mode: 'manual' })

      const banner = wrapper.get('[data-testid="licence-renewal-banner"]')
      expect(banner.attributes('data-renewal-mode')).toBe('manual')
      expect(banner.text()).toContain(formatted(EXPIRES_AT))
      expect(banner.text()).toContain('Team capability')
      expect(wrapper.text()).toContain('13 days left')
    })

    it('manual mode offers both a renew path and a key install path', async () => {
      const wrapper = mountNotice({ licence_state: 'renewal_due', renewal_mode: 'manual' })

      const renew = wrapper.get('[data-testid="licence-renewal-renew-link"]')
      expect(renew.attributes('href')).toBe('https://modulo.run/pricing')

      await wrapper.get('[data-testid="licence-renewal-install-action"]').trigger('click')
      expect(wrapper.emitted('install-key')).toHaveLength(1)
    })

    it('auto mode states the renewal date and never offers a purchase path', () => {
      const wrapper = mountNotice({ licence_state: 'renewal_due', renewal_mode: 'auto' })

      const banner = wrapper.get('[data-testid="licence-renewal-banner"]')
      expect(banner.attributes('data-renewal-mode')).toBe('auto')
      expect(banner.text()).toContain('renews on')
      expect(banner.text()).toContain(formatted(EXPIRES_AT))

      // The load-bearing assertion: a paying customer is never invited to buy
      // again, because a second purchase is a double charge.
      expect(wrapper.find('[data-testid="licence-renewal-renew-link"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="licence-renewal-install-action"]').exists()).toBe(false)
      expect(wrapper.findAll('a')).toHaveLength(0)
    })

    it('omits the day counter when the backend sends no expiry', () => {
      const wrapper = mountNotice({ licence_state: 'renewal_due', days_until_expiry: null })
      expect(wrapper.text()).not.toContain('days left')
    })

    it('renders an em dash when the backend sends no expiry', () => {
      const wrapper = mountNotice({ licence_state: 'renewal_due', expires_at: null })

      const banner = wrapper.get('[data-testid="licence-renewal-banner"]')
      expect(banner.text()).toContain('—')
      expect(banner.text()).not.toContain('null')
    })

    it('falls back to the raw value when the expiry date cannot be parsed', () => {
      const wrapper = mountNotice({ licence_state: 'renewal_due', expires_at: 'not-a-date' })

      expect(wrapper.get('[data-testid="licence-renewal-banner"]').text()).toContain('not-a-date')
    })
  })

  // ── Delivery grace ────────────────────────────────────────────────────

  describe('grace', () => {
    it('shows the modal with both recovery actions at equal prominence', () => {
      const wrapper = mountNotice({ licence_state: 'grace', days_until_expiry: -3 })

      expect(wrapper.find('[data-testid="licence-grace-dialog"]').exists()).toBe(true)
      const install = wrapper.get('[data-testid="licence-grace-install-action"]')
      const buy = wrapper.get('[data-testid="licence-grace-buy-action"]')
      expect(install.text()).toBeTruthy()
      expect(buy.text()).toBeTruthy()
      expect(install.classes()).toContain('flex-1')
      expect(buy.classes()).toContain('flex-1')
      expect(buy.attributes('href')).toBe('https://modulo.run/pricing')
    })

    it('says a key may never have been sent, rather than presuming one exists', () => {
      const wrapper = mountNotice({ licence_state: 'grace', days_until_expiry: -3 })
      expect(wrapper.text()).toContain('expired card')
      expect(wrapper.text()).toContain('no new key was sent')
    })

    it('renders the backend-supplied grace deadline without deriving it', () => {
      const wrapper = mountNotice({ licence_state: 'grace', days_until_expiry: -3 })
      expect(wrapper.text()).toContain(formatted(GRACE_ENDS_AT))
    })

    it('renders an em dash when the backend sends no grace deadline', () => {
      const wrapper = mountNotice({ licence_state: 'grace', days_until_expiry: -3, grace_ends_at: null })

      expect(wrapper.get('[data-testid="licence-grace-dialog"]').text()).toContain('—')
    })

    it('the install action emits install-key', async () => {
      const wrapper = mountNotice({ licence_state: 'grace', days_until_expiry: -3 })
      await wrapper.get('[data-testid="licence-grace-install-action"]').trigger('click')
      expect(wrapper.emitted('install-key')).toHaveLength(1)
    })

    it('is dismissible, but returns on the next visit while the licence is still unrenewed', async () => {
      const first = mountNotice({ licence_state: 'grace', days_until_expiry: -3 })
      expect(first.find('[data-testid="licence-grace-dialog"]').exists()).toBe(true)

      // Dismissing is allowed — the admin must always be able to reach the key
      // input below the dialog.
      first.findComponent({ name: 'Dialog' }).vm.$emit('update:visible', false)
      await nextTick()
      expect(first.find('[data-testid="licence-grace-dialog"]').exists()).toBe(false)

      // A fresh mount (the next visit) with the same backend state re-opens it:
      // the warning is driven by the licence state, not by whether the admin
      // dismissed it.
      const second = mountNotice({ licence_state: 'grace', days_until_expiry: -3 })
      expect(second.find('[data-testid="licence-grace-dialog"]').exists()).toBe(true)
    })
  })

  // ── Lapsed ────────────────────────────────────────────────────────────

  describe('lapsed', () => {
    it('explains that Team capability is off and where to install a key', async () => {
      const wrapper = mountNotice({
        licence_state: 'lapsed',
        has_license: false,
        tier: 'community',
        days_until_expiry: -20,
      })

      const banner = wrapper.get('[data-testid="licence-lapsed-banner"]')
      expect(banner.text()).toContain('Team capability is off')
      expect(banner.text()).toContain('audit log')
      expect(banner.text()).toContain('rest of Modulo keeps working')

      await wrapper.get('[data-testid="licence-lapsed-install-action"]').trigger('click')
      expect(wrapper.emitted('install-key')).toHaveLength(1)
    })

    it('renders without a date when the backend has none', () => {
      const wrapper = mountNotice({ licence_state: 'lapsed', expires_at: null })
      expect(wrapper.get('[data-testid="licence-lapsed-banner"]').text()).toContain('delivery grace has ended')
    })
  })

  // ── Non-actionable states ─────────────────────────────────────────────

  it.each(['none', 'active'])('renders nothing for %s', (licenceState) => {
    const wrapper = mountNotice({ licence_state: licenceState })
    expect(wrapper.find('[data-testid="licence-renewal-banner"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="licence-lapsed-banner"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="licence-grace-dialog"]').exists()).toBe(false)
  })

  it('treats an unknown state as non-actionable rather than guessing', () => {
    const wrapper = mountNotice({ licence_state: 'something-new' })
    expect(wrapper.find('[data-testid="licence-renewal-banner"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="licence-lapsed-banner"]').exists()).toBe(false)
  })
})

describe('LicenseStatusResponse wire shape', () => {
  // Runtime backstop for the compile-time contract above: the generated schema
  // is the source of these names, so a hand-edited fixture cannot drift from
  // what the backend actually serialises.
  const schemaSource = readFileSync(resolve(process.cwd(), 'src/lib/api/schema.ts'), 'utf-8')

  const responseBlock = (() => {
    const start = schemaSource.indexOf('LicenseStatusResponse: {')
    expect(start).toBeGreaterThan(-1)
    const end = schemaSource.indexOf('/** LicenseUploadRequest */', start)
    expect(end).toBeGreaterThan(start)
    return schemaSource.slice(start, end)
  })()

  it.each([
    'licence_state',
    'renewal_mode',
    'days_until_expiry',
    'grace_ends_at',
    'expires_at',
    'term_months',
    'licence_id',
    'issued_at',
  ])('declares the %s field the UI renders', (field) => {
    // Optional fields are emitted as ``name?:``; required ones as ``name:``.
    expect(responseBlock).toMatch(new RegExp(`^\\s*${field}\\??:`, 'm'))
  })
})
