import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'

import AnalyticsFilterBar from '../components/analytics/AnalyticsFilterBar.vue'
import enUS from '../locales/en-US.js'
import type { AnalyticsFilters, AnalyticsMeasure } from '../stores/analytics'

// FAR-1141: the backend's AnalyticsDimension vocabulary now includes
// execution_origin, so the dimension picker must offer it — otherwise the
// aggregate view of dispatched-vs-Modulo-executed runs is unreachable.

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})

function mountBar(filters: Partial<AnalyticsFilters> = {}) {
  return mount(AnalyticsFilterBar, {
    global: { plugins: [i18n] },
    props: {
      filters: { timespan: '7d', groupBy: 'day', dimension: null, ...filters },
      measure: 'count' as AnalyticsMeasure,
      folders: [],
      pipelines: [],
    },
  })
}

function dimensionOptions(wrapper: ReturnType<typeof mountBar>) {
  const select = wrapper.find('[data-testid="analytics-filter-dimension"]')
  expect(select.exists()).toBe(true)
  return select.findAll('option')
}

describe('AnalyticsFilterBar dimension picker (FAR-1141)', () => {
  it('offers execution_origin alongside every existing dimension', () => {
    const options = dimensionOptions(mountBar())
    const values = options.map((o) => o.attributes('value'))

    // additive: every dimension that was offered before is still offered
    expect(values).toContain('trigger_type')
    expect(values).toContain('status')
    expect(values).toContain('pipeline')
    expect(values).toContain('folder')
    expect(values).toContain('team')
    expect(values).toContain('error_code')
    expect(values).toContain('execution_origin')
  })

  it('labels execution_origin from i18n, never hardcoded English', () => {
    const options = dimensionOptions(mountBar())
    const origin = options.find((o) => o.attributes('value') === 'execution_origin')

    expect(origin).toBeDefined()
    const label = origin!.text()
    const messages = enUS as Record<string, any>
    expect(label).toBe(messages.views.AnalyticsView.dimension_execution_origin)
    // the resolved label is real copy, not the raw key leaking through
    expect(label).not.toContain('dimension_execution_origin')
  })

  it('emits the chosen dimension so the aggregate view is actually reachable', async () => {
    const wrapper = mountBar()
    await wrapper.find('[data-testid="analytics-filter-dimension"]').setValue('execution_origin')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { dimension?: string | null }
    expect(last.dimension).toBe('execution_origin')
  })
})
