import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { format, parseISO } from 'date-fns'

vi.mock('../lib/api/client', () => ({
  api: {
    POST: vi.fn(),
  },
}))

import { api } from '../lib/api/client'
import LicenceClaimView from '../views/LicenceClaimView.vue'
import enUS from '../locales/en-US.js'
import type { components } from '../lib/api/schema'

/**
 * CONTRACT: the fixture is the GENERATED OpenAPI response type, not a
 * hand-built shape. A renamed or removed wire field (`license_key`,
 * `expires_at`, `previous_expires_at`, `detail`, `repeated`) fails `vue-tsc`
 * here instead of letting the page drift silently against the real endpoint.
 */
type RenewalClaimResponse = components['schemas']['RenewalClaimResponse']

const strings = (enUS as { views: { LicenceClaimView: Record<string, string> } }).views.LicenceClaimView
const post = api.POST as unknown as ReturnType<typeof vi.fn>

const PURCHASE_KEY = 'PURCHASE-KEY-FROM-EMAIL'
const PREVIOUS_KEY = 'PREVIOUS-LICENCE-KEY'
const STACKED_KEY = 'STACKED-LICENCE-KEY'
const EXPIRES_AT = '2027-06-20T10:00:00Z'
const PREVIOUS_EXPIRES_AT = '2026-12-31T10:00:00Z'

function stackedResponse(overrides: Partial<RenewalClaimResponse> = {}): RenewalClaimResponse {
  return {
    status: 'stacked',
    license_key: STACKED_KEY,
    expires_at: EXPIRES_AT,
    previous_expires_at: PREVIOUS_EXPIRES_AT,
    reason: null,
    detail: 'The unused days on your previous licence were added.',
    repeated: false,
    ...overrides,
  }
}

function unstackedResponse(): RenewalClaimResponse {
  return {
    status: 'unstacked',
    license_key: PURCHASE_KEY,
    expires_at: EXPIRES_AT,
    previous_expires_at: null,
    reason: 'no_presented_key',
    detail: 'No previous licence was applied, so no time was added.',
    repeated: false,
  }
}

function rejection(reason: string) {
  return {
    error: {
      type: `urn:problem:modulo:${reason}`,
      title: 'Validation Error',
      status: 422,
      detail: 'The server explains what happened and what to do next.',
      code: reason,
    },
  }
}

function mountView(): VueWrapper {
  return mount(LicenceClaimView)
}

async function fillAndSubmit(
  wrapper: VueWrapper,
  purchase: string = PURCHASE_KEY,
  previous: string = '',
): Promise<void> {
  await wrapper.get('[data-testid="licence-claim-purchase-key"]').setValue(purchase)
  if (previous !== '') {
    await wrapper.get('[data-testid="licence-claim-previous-key"]').setValue(previous)
  }
  await wrapper.get('form').trigger('submit')
  await flushPromises()
  await nextTick()
}

function formatted(iso: string): string {
  return format(parseISO(iso), 'MMMM d, yyyy')
}

beforeEach(() => {
  vi.clearAllMocks()
  post.mockReset()
})

describe('LicenceClaimView', () => {
  it('POSTs the purchase key and the optional previous licence to the real endpoint', async () => {
    post.mockResolvedValue({ data: stackedResponse(), error: undefined })
    const wrapper = mountView()

    await fillAndSubmit(wrapper, PURCHASE_KEY, PREVIOUS_KEY)

    expect(post).toHaveBeenCalledWith('/api/v1/webhooks/stripe/renewal-claim', {
      body: { purchase_key: PURCHASE_KEY, presented_key: PREVIOUS_KEY },
    })
  })

  it('sends presented_key: null when the previous licence is omitted', async () => {
    post.mockResolvedValue({ data: unstackedResponse(), error: undefined })
    const wrapper = mountView()

    await fillAndSubmit(wrapper)

    expect(post).toHaveBeenCalledWith('/api/v1/webhooks/stripe/renewal-claim', {
      body: { purchase_key: PURCHASE_KEY, presented_key: null },
    })
  })

  it('requires the purchase key before calling the endpoint', async () => {
    const wrapper = mountView()

    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(post).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="licence-claim-error"]').text()).toContain(
      strings.error_purchase_required,
    )
  })

  // ── stacked ────────────────────────────────────────────────────────────

  it('renders the stacked key, the new expiry and the previous expiry from the response', async () => {
    post.mockResolvedValue({ data: stackedResponse(), error: undefined })
    const wrapper = mountView()

    await fillAndSubmit(wrapper, PURCHASE_KEY, PREVIOUS_KEY)

    expect(wrapper.find('[data-testid="licence-claim-stacked"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="licence-claim-key-value"]').text()).toBe(STACKED_KEY)
    expect(wrapper.get('[data-testid="licence-claim-expiry"]').text()).toContain(formatted(EXPIRES_AT))
    expect(wrapper.get('[data-testid="licence-claim-previous-expiry"]').text()).toContain(
      formatted(PREVIOUS_EXPIRES_AT),
    )
    expect(wrapper.get('[data-testid="licence-claim-detail"]').text()).toBe(
      'The unused days on your previous licence were added.',
    )
    expect(wrapper.find('[data-testid="licence-claim-error"]').exists()).toBe(false)
  })

  it('states how many days were carried over from the previous licence', async () => {
    // A fixed future expiry five whole days out: the rendered count is derived
    // from the response's previous_expires_at, never a hard-coded number in the
    // view. Compute the same quantity so the assertion survives the clock.
    const future = new Date(Date.now() + 5 * 86_400_000 + 60_000).toISOString() // nosemgrep: new-date-without-guard
    post.mockResolvedValue({
      data: stackedResponse({ previous_expires_at: future }),
      error: undefined,
    })
    const wrapper = mountView()

    await fillAndSubmit(wrapper, PURCHASE_KEY, PREVIOUS_KEY)

    const expected = Math.ceil((parseISO(future).getTime() - Date.now()) / 86_400_000)
    const text = wrapper.get('[data-testid="licence-claim-days-kept"]').text()
    expect(text).toContain(String(expected))
    expect(text).toContain('previous licence')
  })

  it('marks a repeated claim as already stacked instead of extending again', async () => {
    post.mockResolvedValue({ data: stackedResponse({ repeated: true }), error: undefined })
    const wrapper = mountView()

    await fillAndSubmit(wrapper, PURCHASE_KEY, PREVIOUS_KEY)

    expect(wrapper.get('[data-testid="licence-claim-stacked-title"]').text()).toBe(
      strings.stacked_repeated_title,
    )
    expect(wrapper.get('[data-testid="licence-claim-key-value"]').text()).toBe(STACKED_KEY)
  })

  it('copies the stacked key so the customer can install it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    post.mockResolvedValue({ data: stackedResponse(), error: undefined })
    const wrapper = mountView()

    await fillAndSubmit(wrapper, PURCHASE_KEY, PREVIOUS_KEY)
    await wrapper.get('[data-testid="licence-claim-copy"]').trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledWith(STACKED_KEY)
    expect(wrapper.get('[data-testid="licence-claim-copy"]').text()).toBe(strings.copied)
  })

  // ── unstacked: a legitimate outcome, never an error ────────────────────

  it('presents an unstacked claim as information, not a failure', async () => {
    post.mockResolvedValue({ data: unstackedResponse(), error: undefined })
    const wrapper = mountView()

    await fillAndSubmit(wrapper)

    const panel = wrapper.get('[data-testid="licence-claim-unstacked"]')
    expect(wrapper.find('[data-testid="licence-claim-unstacked"]').exists()).toBe(true)
    expect(panel.text()).toContain(strings.unstacked_title)
    expect(panel.text()).toContain(formatted(EXPIRES_AT))
    expect(panel.text()).toContain('submit again')
    expect(wrapper.get('[data-testid="licence-claim-key-value"]').text()).toBe(PURCHASE_KEY)
    // Crucially not the error surface.
    expect(wrapper.find('[data-testid="licence-claim-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="licence-claim-stacked"]').exists()).toBe(false)
  })

  it('re-opens the form with the purchase key kept so the previous licence can be added', async () => {
    post.mockResolvedValue({ data: unstackedResponse(), error: undefined })
    const wrapper = mountView()

    await fillAndSubmit(wrapper)
    await wrapper.get('[data-testid="licence-claim-again"]').trigger('click')
    await nextTick()

    expect(wrapper.find('form').exists()).toBe(true)
    expect(
      (wrapper.get('[data-testid="licence-claim-purchase-key"]').element as HTMLTextAreaElement).value,
    ).toBe(PURCHASE_KEY)
    expect(
      (wrapper.get('[data-testid="licence-claim-previous-key"]').element as HTMLTextAreaElement).value,
    ).toBe('')
  })

  // ── every rejection reason is actionable prose, never the raw code ─────

  const REASONS = [
    'malformed',
    'signature_mismatch',
    'anchor_mismatch',
    'expired',
    'org_mismatch',
    'same_licence',
    'not_a_purchase_key',
    'no_expiry',
    'no_presented_key',
  ]

  it.each(REASONS)('renders an actionable message for the %s rejection', async (reason) => {
    post.mockResolvedValue(rejection(reason))
    const wrapper = mountView()

    await fillAndSubmit(wrapper, PURCHASE_KEY, PREVIOUS_KEY)

    const message = wrapper.get('[data-testid="licence-claim-error"]').text()
    const expected = strings[`error_${reason}`]
    expect(expected).toBeTruthy()
    expect(message).toContain(expected)
    // The raw machine code must never be what the customer reads.
    expect(message).not.toBe(reason)
    expect(message).not.toContain(`error_${reason}`)
    // The server's own explanation stays available beneath the action.
    expect(wrapper.get('[data-testid="licence-claim-error-detail"]').text()).toBe(
      'The server explains what happened and what to do next.',
    )
  })

  it('renders a generic actionable message for an unknown rejection code', async () => {
    post.mockResolvedValue(rejection('something_new'))
    const wrapper = mountView()

    await fillAndSubmit(wrapper, PURCHASE_KEY, PREVIOUS_KEY)

    expect(wrapper.get('[data-testid="licence-claim-error"]').text()).toContain(strings.error_unknown)
  })

  it('renders an actionable message when the server cannot issue a licence', async () => {
    post.mockResolvedValue(rejection('renewal_claim_unavailable'))
    const wrapper = mountView()

    await fillAndSubmit(wrapper, PURCHASE_KEY, PREVIOUS_KEY)

    expect(wrapper.get('[data-testid="licence-claim-error"]').text()).toContain(
      strings.error_unavailable,
    )
  })

  it('treats a thrown request as an actionable failure, not a crash', async () => {
    post.mockRejectedValue(new Error('network down'))
    const wrapper = mountView()

    await fillAndSubmit(wrapper, PURCHASE_KEY, PREVIOUS_KEY)

    expect(wrapper.get('[data-testid="licence-claim-error"]').text()).toContain(strings.error_unknown)
    expect(wrapper.find('form').exists()).toBe(true)
  })
})
