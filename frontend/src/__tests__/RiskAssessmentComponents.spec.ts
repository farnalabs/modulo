import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'

import ActionBadge from '../components/risk-assessment/ActionBadge.vue'
import RiskAssessmentTable from '../components/risk-assessment/RiskAssessmentTable.vue'
import RiskAssessmentFacets from '../components/risk-assessment/RiskAssessmentFacets.vue'
import RiskAssessmentSummaryCard from '../components/risk-assessment/RiskAssessmentSummaryCard.vue'
import SortIcon from '../components/risk-assessment/SortIcon.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import enUS from '../locales/en-US.js'
import {
  formatApiError,
  getProblemTypeLabel,
  isProblemDetail,
  toProblemDetail,
} from '../lib/api/formatError'
import { formatDateFilename, formatDateShort, formatDateShortWithTime, formatRelativeTime } from '../lib/formatDate'
import {
  RISK_ACTIONS,
  defaultRiskAssessmentFilters,
  type EvidenceRow,
} from '../lib/riskAssessment'

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})

function row(overrides: Partial<EvidenceRow> = {}): EvidenceRow {
  return {
    id: 'e-1',
    organisation_id: 'o-1',
    key: 'risk.api_call',
    subject_type: 'run',
    subject_id: 'run-1',
    value: { status: 'block' },
    observed_at: '2026-01-01T00:00:00Z',
    producer_type: 'run',
    producer_id: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    action: 'block',
    ...overrides,
  }
}

function mountWithI18n(component: unknown, options: Record<string, unknown> = {}) {
  return mount(component as never, { global: { plugins: [i18n] }, ...options })
}

describe('shared formatting used by the risk assessment surface', () => {
  it('covers date formatter fallbacks and valid formats', () => {
    expect(formatDateShort(null)).toBe('—')
    expect(formatDateShort('2026-01-01T00:00:00Z')).toBe('Jan 1, 2026')
    expect(formatDateShortWithTime('invalid')).toBe('—')
    expect(formatDateFilename('2026-01-02T00:00:00Z')).toBe('2026-01-02')
    expect(formatRelativeTime(undefined)).toBe('—')
  })

  it('formats problem details, validation errors, primitives and circular objects', () => {
    const problem = {
      type: 'urn:problem:modulo:forbidden',
      title: 'Forbidden',
      status: 403,
      detail: 'No access',
    }
    expect(isProblemDetail(problem)).toBe(true)
    expect(formatApiError(problem)).toBe('No access')
    expect(getProblemTypeLabel('urn:problem:modulo:forbidden')).toBe('Forbidden')
    expect(getProblemTypeLabel('urn:problem:modulo:unknown')).toBe('Error')
    expect(toProblemDetail(problem)).toBe(problem)
    expect(formatApiError({ detail: [{ msg: 'bad input' }, { msg: 'missing' }] })).toBe('bad input; missing')
    expect(formatApiError({ message: 'message' })).toBe('message')
    expect(formatApiError({ error: 'error' })).toBe('error')
    expect(formatApiError({ title: 'title' })).toBe('title')
    expect(formatApiError(42)).toBe('42')
    expect(formatApiError(true)).toBe('true')
    expect(formatApiError(null)).toBe('Unknown error')
    const circular: Record<string, unknown> = {}
    circular.self = circular
    expect(formatApiError(circular)).toBe('Unknown error')
    expect(toProblemDetail('plain').detail).toBe('plain')
  })

  it('renders PageHeader optional back link, right slot, below-title content, and tabs', () => {
    const wrapper = mountWithI18n(PageHeader, {
      props: {
        title: 'Risk',
        subtitle: 'Assessment',
        backLink: '/runs',
        tabs: [{ label: 'Runs', to: '/runs' }],
      },
      slots: {
        right: '<button>Action</button>',
        default: '<p data-testid="header-context">Context</p>',
      },
      global: {
        plugins: [i18n],
        mocks: { $router: { push: vi.fn() } },
        stubs: { PageTabs: { template: '<nav data-testid="header-tabs"><slot /></nav>' } },
      },
    })

    expect(wrapper.find('h1').text()).toBe('Risk')
    expect(wrapper.find('p').text()).toContain('Assessment')
    expect(wrapper.find('[data-testid="header-context"]').text()).toBe('Context')
    expect(wrapper.findAll('button').at(-1)?.text()).toBe('Action')
    expect(wrapper.find('[data-testid="header-tabs"]').exists()).toBe(true)
  })

  it('renders ErrorAlert problem, retry and both dismiss variants', async () => {
    const retry = vi.fn()
    const dismiss = vi.fn()
    const problemWrapper = mountWithI18n(ErrorAlert, {
      props: { message: { type: 'x', title: 'Problem', status: 400, detail: 'Bad' }, onRetry: retry, onDismiss: dismiss, dismissLabel: 'Close', dismissIcon: true },
    })
    await problemWrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    await problemWrapper.find('[data-testid="error-alert-dismiss"]').trigger('click')
    expect(problemWrapper.text()).toContain('Problem: Bad')
    expect(retry).toHaveBeenCalledOnce()
    expect(dismiss).toHaveBeenCalledOnce()

    const textWrapper = mountWithI18n(ErrorAlert, {
      props: { message: 'Plain', onDismiss: dismiss, dismissLabel: 'Close' },
    })
    expect(textWrapper.text()).toContain('Plain')
    expect(textWrapper.find('[data-testid="error-alert-dismiss"]').element.tagName).toBe('BUTTON')
  })
})

describe('ActionBadge', () => {
  it.each([
    ['block', 'Block', 'text-destructive-text'],
    ['warn', 'Warn', 'text-warning-text'],
    ['continue', 'Continue', 'text-success-text'],
    ['escalate', 'Escalate', 'text-primary-text'],
  ])('renders %s with its accessible text-safe class', (action, label, cls) => {
    const wrapper = mountWithI18n(ActionBadge, { props: { action } })

    expect(wrapper.text()).toBe(label)
    expect(wrapper.classes().join(' ')).toContain(cls)
  })

  it('falls back to the raw action when no translation exists', () => {
    const wrapper = mountWithI18n(ActionBadge, { props: { action: 'mystery' } })

    // vue-i18n returns the key on a miss; the badge must not show that key.
    expect(wrapper.text()).toBe('mystery')
    expect(wrapper.classes().join(' ')).toContain('text-muted-foreground')
  })
})

describe('SortIcon', () => {
  it('renders a distinct glyph per direction', () => {
    const asc = mountWithI18n(SortIcon, { props: { direction: 'asc' } })
    const desc = mountWithI18n(SortIcon, { props: { direction: 'desc' } })
    const none = mountWithI18n(SortIcon, { props: { direction: 'none' } })

    expect(asc.find('path').attributes('d')).toContain('M5 10')
    expect(desc.find('path').attributes('d')).toContain('M19 14')
    expect(none.find('path').attributes('d')).toContain('M7 16')
  })
})

describe('RiskAssessmentTable', () => {
  it('renders a row per evidence fact with the server-resolved action', () => {
    const wrapper = mountWithI18n(RiskAssessmentTable, {
      props: { rows: [row(), row({ id: 'e-2', key: 'assessment.score', action: null })] },
    })

    const rows = wrapper.findAll('[data-testid="risk-assessment-row"]')
    expect(rows).toHaveLength(2)
    const badges = rows.map((r) => r.find('[data-testid="risk-assessment-action-badge"]'))
    expect(badges[0].text()).toBe('Block')
    // No decision record → an explicit unknown, never a key-prefix guess.
    expect(badges[1].text()).toBe('Unknown')
  })

  it('shows the empty state when there are no rows', () => {
    const wrapper = mountWithI18n(RiskAssessmentTable, { props: { rows: [] } })

    expect(wrapper.find('[data-testid="risk-assessment-empty"]').text()).toContain('No evidence rows')
  })

  it('exposes accessible sort controls with aria-sort', async () => {
    const wrapper = mountWithI18n(RiskAssessmentTable, {
      props: {
        rows: [
          row({ id: 'b', key: 'b.key', subject_id: 'run-b', created_at: '2026-01-02T00:00:00Z' }),
          row({ id: 'a', key: 'a.key', subject_id: 'run-a', created_at: '2026-01-01T00:00:00Z' }),
        ],
      },
    })

    const timestampHeader = wrapper.find('[data-testid="risk-assessment-sort-timestamp"]')
    // A real button is the keyboard affordance — not a click-only <th>.
    expect(timestampHeader.element.tagName).toBe('BUTTON')
    expect(timestampHeader.element.closest('th')?.getAttribute('aria-sort')).toBe('descending')

    const keyButton = wrapper.find('[data-testid="risk-assessment-sort-key"]')
    expect(keyButton.element.closest('th')?.getAttribute('aria-sort') ?? undefined).toBeUndefined()

    await keyButton.trigger('click')
    expect(keyButton.element.closest('th')?.getAttribute('aria-sort')).toBe('descending')

    await keyButton.trigger('click')
    expect(keyButton.element.closest('th')?.getAttribute('aria-sort')).toBe('ascending')
    const ascendingKeys = wrapper
      .findAll('[data-testid="risk-assessment-row"]')
      .map((r) => r.findAll('td')[1].text())
    expect(ascendingKeys).toEqual(['a.key', 'b.key'])

    await keyButton.trigger('click')
    expect(keyButton.element.closest('th')?.getAttribute('aria-sort')).toBe('descending')
    const descendingKeys = wrapper
      .findAll('[data-testid="risk-assessment-row"]')
      .map((r) => r.findAll('td')[1].text())
    expect(descendingKeys).toEqual(['b.key', 'a.key'])
  })

  it('sorts by timestamp, action and subject', async () => {
    const wrapper = mountWithI18n(RiskAssessmentTable, {
      props: {
        rows: [
          row({ id: '1', key: 'k1', subject_id: 's-b', action: 'warn', created_at: '2026-01-01T00:00:00Z' }),
          row({ id: '2', key: 'k2', subject_id: 's-a', action: 'block', created_at: '2026-01-03T00:00:00Z' }),
        ],
      },
    })

    const ids = () =>
      wrapper.findAll('[data-testid="risk-assessment-row"]').map((r) => r.findAll('td')[1].text())

    await wrapper.find('[data-testid="risk-assessment-sort-timestamp"]').trigger('click')
    // default desc then first click flips to asc
    expect(ids()).toEqual(['k1', 'k2'])

    await wrapper.find('[data-testid="risk-assessment-sort-action"]').trigger('click')
    // Descending alphabetical: 'warn' sorts after 'block', so it lands first.
    expect(ids()).toEqual(['k1', 'k2'])

    await wrapper.find('[data-testid="risk-assessment-sort-subject"]').trigger('click')
    expect(ids()).toEqual(['k1', 'k2'])
  })

  it('formats string and object values without throwing on circular input', () => {
    const circular: Record<string, unknown> = {}
    circular.self = circular
    const wrapper = mountWithI18n(RiskAssessmentTable, {
      props: {
        rows: [
          row({ id: 's', value: 'plain-text' }),
          row({ id: 'o', value: { a: 1 } }),
          row({ id: 'c', value: circular }),
          row({ id: 'u', value: undefined }),
        ],
      },
    })

    const values = wrapper.findAll('pre').map((p) => p.text())
    expect(values[0]).toBe('plain-text')
    expect(values[1]).toContain('"a": 1')
    expect(values[2]).toBe('[object Object]')
    expect(values[3]).toBe('undefined')
  })

  it('covers all table sort comparison directions and invalid dates', async () => {
    const wrapper = mountWithI18n(RiskAssessmentTable, {
      props: {
        rows: [
          row({ id: 'a', key: 'a.key', action: 'continue', subject_id: 'subject-a', created_at: 'invalid' }),
          row({ id: 'b', key: 'b.key', action: 'block', subject_id: 'subject-b', created_at: '2026-01-02T00:00:00Z' }),
          row({ id: 'c', key: 'c.key', action: 'warn', subject_id: 'subject-c', created_at: '2026-01-01T00:00:00Z' }),
        ],
      },
    })

    const keys = () => wrapper.findAll('[data-testid="risk-assessment-row"]').map((r) => r.findAll('td')[1].text())
    await wrapper.find('[data-testid="risk-assessment-sort-key"]').trigger('click')
    expect(keys()).toEqual(['c.key', 'b.key', 'a.key'])
    await wrapper.find('[data-testid="risk-assessment-sort-action"]').trigger('click')
    expect(keys()).toEqual(['c.key', 'a.key', 'b.key'])
    await wrapper.find('[data-testid="risk-assessment-sort-subject"]').trigger('click')
    expect(keys()).toEqual(['c.key', 'b.key', 'a.key'])
  })
})

describe('RiskAssessmentSummaryCard', () => {
  it('renders counts, degradation and the time window', () => {
    const wrapper = mountWithI18n(RiskAssessmentSummaryCard, {
      props: {
        totalRows: 9,
        actionCounts: { block: 1, warn: 2, continue: 3, escalate: 4 },
        facetDegraded: true,
         timeWindow: 30,
         dataTestId: 'custom-risk-summary',
       },
    })

     expect(wrapper.find('[data-testid="custom-risk-summary"]').exists()).toBe(true)
     expect(wrapper.find('[data-testid="risk-assessment-summary-card"]').exists()).toBe(false)
     expect(wrapper.find('[data-testid="risk-assessment-total"]').text()).toBe('9')
    expect(wrapper.find('[data-testid="risk-assessment-block-count"]').text()).toBe('1')
    expect(wrapper.find('[data-testid="risk-assessment-warn-count"]').text()).toBe('2')
    expect(wrapper.find('[data-testid="risk-assessment-continue-count"]').text()).toBe('3')
    expect(wrapper.find('[data-testid="risk-assessment-escalate-count"]').text()).toBe('4')
    expect(wrapper.find('[data-testid="risk-assessment-facet-degraded"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="risk-assessment-time-window"]').text()).toContain('30')
  })

  it('defaults missing counts to zero and hides chrome when healthy', () => {
    const wrapper = mountWithI18n(RiskAssessmentSummaryCard, {
      props: { totalRows: 0, actionCounts: {}, facetDegraded: false, timeWindow: 0 },
    })

    expect(wrapper.find('[data-testid="risk-assessment-block-count"]').text()).toBe('0')
    expect(wrapper.find('[data-testid="risk-assessment-facet-degraded"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="risk-assessment-time-window"]').exists()).toBe(false)
  })
})

describe('RiskAssessmentFacets', () => {
  function mountFacets(overrides: Record<string, unknown> = {}) {
    return mountWithI18n(RiskAssessmentFacets, {
      props: {
        filters: defaultRiskAssessmentFilters(),
        facetDegraded: false,
        isLoading: false,
        pipelines: [
          { id: 'p-1', name: 'Alpha' },
          { id: 'p-2', name: 'Beta' },
        ],
        ...overrides,
      },
    })
  }

  it('offers every pipeline and every gate action from the shared vocabulary', () => {
    const wrapper = mountFacets()

    const pipelineOptions = wrapper
      .find('[data-testid="risk-assessment-pipeline"]')
      .findAll('option')
      .map((o) => o.text())
    expect(pipelineOptions).toEqual(['All pipelines', 'Alpha', 'Beta'])

    const actionValues = wrapper
      .find('[data-testid="risk-assessment-action"]')
      .findAll('option')
      .map((o) => o.attributes('value'))
    expect(actionValues).toEqual(['', ...RISK_ACTIONS])
  })

  it('emits the updated filters as the operator edits', async () => {
    const wrapper = mountFacets()

    await wrapper.find('[data-testid="risk-assessment-key-prefix"]').setValue('risk.')
    const emitted = wrapper.emitted('update:filters')
    expect(emitted?.at(-1)?.[0]).toMatchObject({ keyPrefix: 'risk.' })
  })

  it('emits the pipeline selection as part of the filters', async () => {
    const wrapper = mountFacets()

    await wrapper.find('[data-testid="risk-assessment-pipeline"]').setValue('p-2')

    expect(wrapper.emitted('update:filters')?.at(-1)?.[0]).toMatchObject({ pipelineId: 'p-2' })
  })

  it('emits apply with the current filters', async () => {
    const wrapper = mountFacets()

    await wrapper.find('[data-testid="risk-assessment-days"]').setValue('30')
    await wrapper.find('[data-testid="risk-assessment-apply"]').trigger('click')

    expect(wrapper.emitted('apply')?.at(-1)?.[0]).toMatchObject({ days: 30 })
  })

  it('resets to the shared defaults and emits reset', async () => {
    const wrapper = mountFacets({
      filters: { ...defaultRiskAssessmentFilters(), keyPrefix: 'risk.', action: 'block' },
    })

    await wrapper.find('[data-testid="risk-assessment-reset"]').trigger('click')

    expect(wrapper.emitted('reset')).toHaveLength(1)
    expect((wrapper.find('[data-testid="risk-assessment-key-prefix"]').element as HTMLInputElement).value).toBe('')
  })

  it('disables the action facet and explains why when degraded', () => {
    const wrapper = mountFacets({ facetDegraded: true })

    expect(
      wrapper.find('[data-testid="risk-assessment-action"]').attributes('disabled'),
    ).toBeDefined()
    expect(wrapper.find('[data-testid="risk-assessment-facet-degraded-note"]').exists()).toBe(true)
  })

  it('re-syncs its local copy when the parent replaces the filters', async () => {
    const wrapper = mount(RiskAssessmentFacets, {
      global: { plugins: [i18n] },
      props: {
        filters: defaultRiskAssessmentFilters(),
        facetDegraded: false,
        isLoading: false,
        pipelines: [],
      },
    })

    await wrapper.setProps({
      filters: { ...defaultRiskAssessmentFilters(), keyPrefix: 'assessment.', days: 90 },
    })

    expect(
      (wrapper.find('[data-testid="risk-assessment-key-prefix"]').element as HTMLInputElement).value,
    ).toBe('assessment.')
    expect(
      (wrapper.find('[data-testid="risk-assessment-days"]').element as HTMLSelectElement).value,
    ).toBe('90')
  })

  it('surfaces a pipeline load failure instead of hiding it', () => {
    const wrapper = mountFacets({ pipelines: [], pipelinesError: 'HTTP 503' })

    expect(wrapper.find('[data-testid="risk-assessment-pipelines-error-message"]').text()).toBe('HTTP 503')
  })
})
