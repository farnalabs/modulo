import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import RunErrorTag from '../components/shared/RunErrorTag.vue'

// The `model` entry in CLASS_STYLES uses the destructive pill, the same tokens
// as the other critical classes (agent / eval). Asserting the EXACT tokens (not
// merely "some class") is what makes dropping the `model` entry fail: the
// lookup would fall through to the muted fallback.
const DESTRUCTIVE_PILL = ['bg-destructive/10', 'text-destructive-text']

type ModelErrorCase = { code: string; label: string; titleIncludes?: string }

// Every `model.*` code resolves its pill through the same `model` entry,
// because the lookup is keyed on the code's FIRST dotted segment. Each also
// carries its own i18n label, so a code that lost its translation would render
// "Unknown error" instead.
const MODEL_ERROR_CODES: ModelErrorCase[] = [
  { code: 'model.provider_timeout', label: 'Model timed out' },
  { code: 'model.connection', label: 'Model connection error' },
  { code: 'model.rate_limited', label: 'Model rate limited' },
  {
    code: 'model.insufficient_funds',
    label: 'Model provider out of funds',
    // Billing fault: the tooltip must tell the operator how to clear it.
    titleIncludes: 'Top up the provider account',
  },
]

describe('RunErrorTag', () => {
  it('renders the i18n label for a known dotted code', () => {
    const wrapper = mount(RunErrorTag, { props: { code: 'agent.stall' } })
    expect(wrapper.text()).toBe('Worker stall')
  })

  it('falls back to Unknown error for an unknown code', () => {
    const wrapper = mount(RunErrorTag, { props: { code: 'some.mystery' } })
    expect(wrapper.text()).toBe('Unknown error')
  })

  it('renders an em dash when code is falsy', () => {
    const wrapper = mount(RunErrorTag, { props: { code: null } })
    expect(wrapper.text()).toBe('—')
  })

  it('applies the class for the code class group (agent destructive vs capacity primary)', () => {
    const agent = mount(RunErrorTag, { props: { code: 'agent.failed' } })
    expect(agent.classes()).toContain('text-destructive-text')
    const capacity = mount(RunErrorTag, { props: { code: 'capacity.org' } })
    expect(capacity.classes()).toContain('text-primary-text')
  })

  it('sets the detail title tooltip', () => {
    const wrapper = mount(RunErrorTag, { props: { code: 'node.timeout', detail: 'hit the timeout guard' } })
    expect(wrapper.attributes('title')).toContain('hit the timeout guard')
  })

  it('uses the errorCodeDescription for the tooltip when detail is absent', () => {
    const wrapper = mount(RunErrorTag, { props: { code: 'agent.stall' } })
    const title = wrapper.attributes('title') ?? ''
    expect(title).toContain('recovered by re-dispatch')
  })

  it('renders provider codes with a warning-style pill and their i18n label', () => {
    const wrapper = mount(RunErrorTag, { props: { code: 'provider.rate_limited' } })
    expect(wrapper.text()).toBe('Provider rate limited')
    expect(wrapper.classes()).toContain('text-warning-text')
    const auth = mount(RunErrorTag, { props: { code: 'provider.authentication' } })
    expect(auth.text()).toBe('Authentication failed')
  })

  it('renders script error codes with their i18n labels', () => {
    const failed = mount(RunErrorTag, { props: { code: 'script.failed' } })
    expect(failed.text()).toBe('Script failed')
    const invalid = mount(RunErrorTag, { props: { code: 'script.invalid_output' } })
    expect(invalid.text()).toBe('Invalid script output')
    const sideEffect = mount(RunErrorTag, { props: { code: 'script.side_effect_unknown' } })
    expect(sideEffect.text()).toBe('Script side effects unknown')
    const sessionLost = mount(RunErrorTag, { props: { code: 'script.session_lost' } })
    expect(sessionLost.text()).toBe('Session lost')
  })

  it('renders connector error codes with their i18n labels and descriptions', () => {
    const failed = mount(RunErrorTag, { props: { code: 'connector.failed' } })
    expect(failed.text()).toBe('Connector failed')
    expect(failed.text()).not.toBe('Unknown error')
    expect(failed.classes()).toContain('text-warning-text')
    expect(failed.attributes('title')).toContain('The connector reported that its operation failed.')

    const unknown = mount(RunErrorTag, { props: { code: 'connector.side_effect_unknown' } })
    expect(unknown.text()).toBe('Connector side effects unknown')
    expect(unknown.attributes('title')).toContain('upstream side-effect state is unknown')
  })

  it.each(MODEL_ERROR_CODES)(
    'renders $code with its own label and the shared destructive model.* pill',
    ({ code, label, titleIncludes }) => {
      const wrapper = mount(RunErrorTag, { props: { code } })
      expect(wrapper.text()).toBe(label)
      expect(wrapper.text()).not.toBe('Unknown error')
      expect(wrapper.classes()).toContain(DESTRUCTIVE_PILL[0])
      expect(wrapper.classes()).toContain(DESTRUCTIVE_PILL[1])
      if (titleIncludes) {
        expect(wrapper.attributes('title')).toContain(titleIncludes)
      }
    },
  )

  // The `model` entry is keyed on the first dotted segment, so an unrecognised
  // sub-code under model.* still gets the destructive pill (it has no label, so
  // the fallback text is expected). Pinning this proves the styling is
  // class-keyed by the first segment, not an enumeration of the four known
  // model.* codes — the property that keeps sibling codes styled if one is
  // renamed or a new one is added.
  it('keys the model.* pill on the first dotted segment, not the full code', () => {
    const canonical = mount(RunErrorTag, { props: { code: 'model.insufficient_funds' } })
    const unknownSubcode = mount(RunErrorTag, { props: { code: 'model.some_future_fault' } })
    expect(unknownSubcode.classes()).toStrictEqual(canonical.classes())
    // ...and that shared entry is the destructive pill, not the muted fallback
    // (asserting only "same as canonical" would pass if BOTH fell back).
    expect(unknownSubcode.classes()).toContain(DESTRUCTIVE_PILL[0])
    expect(unknownSubcode.classes()).toContain(DESTRUCTIVE_PILL[1])

    // A first segment with no entry falls back to muted, so the assertion above
    // is not satisfied by every code sharing one style.
    const mutedFallback = mount(RunErrorTag, { props: { code: 'some.mystery' } })
    expect(mutedFallback.classes()).toContain('text-muted-foreground')
    expect(mutedFallback.classes()).not.toContain('text-destructive-text')
  })
})
