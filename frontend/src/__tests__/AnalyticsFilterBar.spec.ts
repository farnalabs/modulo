import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'

import AnalyticsFilterBar from '../components/analytics/AnalyticsFilterBar.vue'
import enUS from '../locales/en-US.js'
import { applyQueryParamsToFilters, type AnalyticsFilters, type AnalyticsMeasure } from '../stores/analytics'

// FAR-1141: the backend's AnalyticsDimension vocabulary now includes
// execution_origin, so the dimension picker must offer it — otherwise the
// aggregate view of dispatched-vs-Modulo-executed runs is unreachable.

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})

function mountBar(
  filters: Partial<AnalyticsFilters> = {},
  options: { measure?: AnalyticsMeasure; folders?: { id: string; name: string }[]; pipelines?: { id: string; name: string }[] } = {},
) {
  return mount(AnalyticsFilterBar, {
    global: { plugins: [i18n] },
    props: {
      filters: { timespan: '7d', groupBy: 'day', dimension: null, ...filters },
      measure: options.measure ?? 'count',
      folders: options.folders ?? [],
      pipelines: options.pipelines ?? [],
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

describe('AnalyticsFilterBar dimension picker (FAR-1567: trigger_id)', () => {
  it('offers trigger_id as a selectable dimension', () => {
    const values = dimensionOptions(mountBar()).map((o) => o.attributes('value'))

    expect(values).toContain('trigger_id')
    expect(values).toContain('trigger_type')
  })

  it('labels trigger_id from i18n, never hardcoded English', () => {
    const options = dimensionOptions(mountBar())
    const triggerId = options.find((o) => o.attributes('value') === 'trigger_id')

    expect(triggerId).toBeDefined()
    const label = triggerId!.text()
    const messages = enUS as Record<string, any>
    expect(label).toBe(messages.views.AnalyticsView.dimension_trigger_id)
    expect(label).not.toContain('dimension_trigger_id')
  })

  it('emits trigger_id when it is chosen', async () => {
    const wrapper = mountBar()
    await wrapper.find('[data-testid="analytics-filter-dimension"]').setValue('trigger_id')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { dimension?: string | null }
    expect(last.dimension).toBe('trigger_id')
  })

  it('shows a ?dimension=trigger_id deep link as the selected option', () => {
    // Deep link -> store filters (the store keeps trigger_id) -> rendered
    // select. Before FAR-1567 the option was missing, so the select silently
    // fell back to the empty "Overall" value despite the store holding it.
    const { filters, applied } = applyQueryParamsToFilters(
      { dimension: 'trigger_id' },
      { timespan: '7d', groupBy: 'day' },
    )
    expect(applied).toBe(true)
    expect(filters.dimension).toBe('trigger_id')

    const select = mountBar(filters).find('[data-testid="analytics-filter-dimension"]')
    expect((select.element as HTMLSelectElement).value).toBe('trigger_id')
  })
})

describe('AnalyticsFilterBar timespan buttons', () => {
  it('renders all timespan buttons', () => {
    const wrapper = mountBar()
    const buttons = wrapper.findAll('[data-testid^="analytics-timespan-"]')
    expect(buttons.length).toBeGreaterThan(0)
    // Check for common timespans
    expect(wrapper.find('[data-testid="analytics-timespan-1h"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="analytics-timespan-24h"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="analytics-timespan-7d"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="analytics-timespan-30d"]').exists()).toBe(true)
  })

  it('highlights the active timespan button', () => {
    const wrapper = mountBar({ timespan: '7d' })
    const activeBtn = wrapper.find('[data-testid="analytics-timespan-7d"]')
    expect(activeBtn.classes()).toContain('bg-primary')
    expect(activeBtn.classes()).toContain('text-primary-foreground')
  })

  it('emits update:filters when timespan button is clicked', async () => {
    const wrapper = mountBar({ timespan: '7d' })
    await wrapper.find('[data-testid="analytics-timespan-24h"]').trigger('click')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { timespan?: string }
    expect(last.timespan).toBe('24h')
  })

  it('preserves other filters when timespan changes', async () => {
    const wrapper = mountBar({ timespan: '7d', groupBy: 'week', dimension: 'status' })
    await wrapper.find('[data-testid="analytics-timespan-1h"]').trigger('click')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { timespan?: string; groupBy?: string; dimension?: string }
    expect(last.timespan).toBe('1h')
    expect(last.groupBy).toBe('week')
    expect(last.dimension).toBe('status')
  })
})

describe('AnalyticsFilterBar groupBy buttons', () => {
  it('renders day and week groupBy buttons when not hour granular', () => {
    const wrapper = mountBar({ timespan: '7d' })
    expect(wrapper.find('[data-testid="analytics-group-by-day"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="analytics-group-by-week"]').exists()).toBe(true)
  })

  it('shows disabled hour button when timespan is hour granular', () => {
    const wrapper = mountBar({ timespan: '1h' })
    const hourBtn = wrapper.find('[data-testid="analytics-group-by-hour"]')
    expect(hourBtn.exists()).toBe(true)
    expect(hourBtn.attributes('disabled')).toBeDefined()
    expect(hourBtn.attributes('aria-disabled')).toBe('true')
  })

  it('does not render day/week buttons when hour granular', () => {
    const wrapper = mountBar({ timespan: '1h' })
    expect(wrapper.find('[data-testid="analytics-group-by-day"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="analytics-group-by-week"]').exists()).toBe(false)
  })

  it('highlights active groupBy button', () => {
    const wrapper = mountBar({ groupBy: 'week' })
    const activeBtn = wrapper.find('[data-testid="analytics-group-by-week"]')
    expect(activeBtn.classes()).toContain('bg-primary')
  })

  it('emits update:filters when groupBy button is clicked', async () => {
    const wrapper = mountBar({ groupBy: 'day' })
    await wrapper.find('[data-testid="analytics-group-by-week"]').trigger('click')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { groupBy?: string }
    expect(last.groupBy).toBe('week')
  })
})

describe('AnalyticsFilterBar measure buttons', () => {
  it('renders all measure buttons', () => {
    const wrapper = mountBar()
    const buttons = wrapper.findAll('[data-testid^="analytics-measure-"]')
    expect(buttons.length).toBeGreaterThan(0)
  })

  it('highlights active measure button', () => {
    const wrapper = mountBar({}, { measure: 'count' })
    const activeBtn = wrapper.find('[data-testid="analytics-measure-count"]')
    expect(activeBtn.classes()).toContain('bg-primary')
  })

  it('emits update:measure when measure button is clicked', async () => {
    const wrapper = mountBar({}, { measure: 'count' })
    const btn = wrapper.find('[data-testid="analytics-measure-duration"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')

    const emitted = wrapper.emitted('update:measure')
    expect(emitted).toBeTruthy()
    expect(emitted![0][0]).toBe('duration')
  })
})

describe('AnalyticsFilterBar filter select handlers', () => {
  it('emits update:filters when dimension select changes', async () => {
    const wrapper = mountBar()
    const select = wrapper.find('[data-testid="analytics-filter-dimension"]')
    const el = select.element as HTMLSelectElement
    el.value = 'status'
    await select.trigger('change')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { dimension?: string }
    expect(last.dimension).toBe('status')
  })

  it('emits update:filters with null when dimension is cleared', async () => {
    const wrapper = mountBar({ dimension: 'status' })
    const select = wrapper.find('[data-testid="analytics-filter-dimension"]')
    const el = select.element as HTMLSelectElement
    el.value = ''
    await select.trigger('change')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { dimension?: string | null }
    expect(last.dimension).toBeNull()
  })

  it('emits update:filters when folder select changes via handler', async () => {
    const wrapper = mountBar({}, { folders: [{ id: 'f1', name: 'Folder 1' }] })
    ;(wrapper.vm as unknown as { onFolderChange: (e: Event) => void }).onFolderChange({
      target: { value: 'f1' },
    } as unknown as Event)

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { folderId?: string }
    expect(last.folderId).toBe('f1')
  })

  it('emits update:filters with null when folder is cleared via handler', async () => {
    const wrapper = mountBar({ folderId: 'f1' })
    ;(wrapper.vm as unknown as { onFolderChange: (e: Event) => void }).onFolderChange({
      target: { value: '' },
    } as unknown as Event)

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { folderId?: string | null }
    expect(last.folderId).toBeNull()
  })

  it('emits update:filters when pipeline select changes via handler', async () => {
    const wrapper = mountBar({}, { pipelines: [{ id: 'p1', name: 'Pipeline 1' }] })
    ;(wrapper.vm as unknown as { onPipelineChange: (e: Event) => void }).onPipelineChange({
      target: { value: 'p1' },
    } as unknown as Event)

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { pipelineId?: string }
    expect(last.pipelineId).toBe('p1')
  })

  it('emits update:filters when trigger type select changes', async () => {
    const wrapper = mountBar()
    await wrapper.find('[data-testid="analytics-filter-trigger-type"]').setValue('cron')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { triggerType?: string }
    expect(last.triggerType).toBe('cron')
  })

  it('emits update:filters when status select changes', async () => {
    const wrapper = mountBar()
    await wrapper.find('[data-testid="analytics-filter-status"]').setValue('failed')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { status?: string }
    expect(last.status).toBe('failed')
  })

  it('emits update:filters when error code input changes', async () => {
    const wrapper = mountBar()
    const input = wrapper.find('[data-testid="analytics-filter-error-code"]')
    await input.setValue('provider.rate_limit')
    await input.trigger('input')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { errorCode?: string }
    expect(last.errorCode).toBe('provider.rate_limit')
  })

  it('emits update:filters with null when error code is cleared', async () => {
    const wrapper = mountBar({ errorCode: 'some_error' })
    const input = wrapper.find('[data-testid="analytics-filter-error-code"]')
    await input.setValue('')
    await input.trigger('input')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { errorCode?: string | null }
    expect(last.errorCode).toBeNull()
  })

  it('trims error code input value', async () => {
    const wrapper = mountBar()
    const input = wrapper.find('[data-testid="analytics-filter-error-code"]')
    await input.setValue('  provider.rate_limit  ')
    await input.trigger('input')

    const emitted = wrapper.emitted('update:filters')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0] as { errorCode?: string }
    expect(last.errorCode).toBe('provider.rate_limit')
  })

  it('preserves other filters when any filter changes', async () => {
    const wrapper = mountBar({
      timespan: '7d',
      groupBy: 'day',
      dimension: 'status',
      folderId: 'f1',
      pipelineId: 'p1',
      triggerType: 'manual',
      status: 'failed',
      errorCode: 'some_error',
    })
    await wrapper.find('[data-testid="analytics-filter-status"]').setValue('complete')

    const emitted = wrapper.emitted('update:filters')
    const last = emitted![emitted!.length - 1][0] as Record<string, unknown>
    expect(last.status).toBe('complete')
    expect(last.dimension).toBe('status')
    expect(last.folderId).toBe('f1')
    expect(last.pipelineId).toBe('p1')
    expect(last.triggerType).toBe('manual')
    expect(last.errorCode).toBe('some_error')
  })
})

describe('AnalyticsFilterBar i18n labels', () => {
  it('uses i18n for timespan labels', () => {
    const wrapper = mountBar()
    const btn = wrapper.find('[data-testid="analytics-timespan-7d"]')
    expect(btn.text()).not.toContain('timespan_7d')
  })

  it('uses i18n for groupBy labels', () => {
    const wrapper = mountBar()
    const btn = wrapper.find('[data-testid="analytics-group-by-day"]')
    expect(btn.text()).not.toContain('group_by_day')
  })

  it('uses i18n for measure labels', () => {
    const wrapper = mountBar()
    const btn = wrapper.find('[data-testid="analytics-measure-count"]')
    expect(btn.text()).not.toContain('measure_')
  })

  it('uses i18n for dimension option labels', () => {
    const wrapper = mountBar()
    const select = wrapper.find('[data-testid="analytics-filter-dimension"]')
    const options = select.findAll('option')
    for (const opt of options) {
      const label = opt.text()
      expect(label).not.toContain('dimension_')
    }
  })

  it('uses i18n for folder option "All"', () => {
    const wrapper = mountBar({}, { folders: [{ id: 'f1', name: 'Folder 1' }] })
    const select = wrapper.find('[data-testid="analytics-filter-folder"]')
    const allOption = select.find('option[value=""]')
    expect(allOption.exists()).toBe(true)
    expect(allOption.text()).toBe('All')
  })
})
