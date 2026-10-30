import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'
import SystemMapDraftReview from '../components/system-maps/SystemMapDraftReview.vue'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

describe('SystemMapDraftReview', () => {
  it('explains that draft review waits for the discovery API and does not publish', () => {
    const wrapper = mount(SystemMapDraftReview, { global: { plugins: [i18n] } })

    expect(wrapper.get('[data-testid="system-map-draft-review"]').text()).toContain('System discovery is not available')
    expect(wrapper.text()).toContain('publishing will appear here only after the discovery API is available')
  })
})
