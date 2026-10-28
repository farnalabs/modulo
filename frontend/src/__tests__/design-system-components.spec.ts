import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import BaseCard from '../components/ui/BaseCard.vue'
import CardHeader from '../components/ui/CardHeader.vue'
import DataCard from '../components/ui/DataCard.vue'
import MetricCard from '../components/ui/MetricCard.vue'
import SummaryCard from '../components/ui/SummaryCard.vue'
import SectionCard from '../components/shared/SectionCard.vue'

describe('BaseCard', () => {
  it('maps visual props, custom classes, test ids, and named slots', () => {
    const wrapper = mount(BaseCard, {
      props: {
        variant: 'bordered',
        padding: 'lg',
        elevation: 'sm',
        class: 'custom-card',
        dataTestId: 'base-card',
      },
      slots: {
        header: '<header data-testid="base-header">Header</header>',
        default: '<main data-testid="base-content">Content</main>',
        footer: '<footer data-testid="base-footer">Footer</footer>',
      },
    })

    expect(wrapper.attributes('data-testid')).toBe('base-card')
    expect(wrapper.classes()).toEqual(expect.arrayContaining(['bg-card', 'border', 'p-6', 'shadow-sm', 'custom-card']))
    expect(wrapper.find('[data-testid="base-header"]').text()).toBe('Header')
    expect(wrapper.find('[data-testid="base-content"]').text()).toBe('Content')
    expect(wrapper.find('[data-testid="base-footer"]').text()).toBe('Footer')
  })
})

describe('CardHeader and DataCard', () => {
  it('renders title, description, actions, and content/footer slot wrappers', () => {
    const wrapper = mount(DataCard, {
      props: {
        title: 'Details',
        description: 'Useful context',
        dataTestId: 'data-card',
      },
      slots: {
        headerActions: '<button data-testid="header-action">Edit</button>',
        default: '<p data-testid="card-content">Body</p>',
        footer: '<p data-testid="card-footer">Footer</p>',
      },
    })

    expect(wrapper.find('[data-testid="data-card-header"] h3').text()).toBe('Details')
    expect(wrapper.find('[data-testid="data-card-header"] p').text()).toBe('Useful context')
    expect(wrapper.find('[data-testid="header-action"]').text()).toBe('Edit')
    expect(wrapper.find('[data-testid="data-card-content"] [data-testid="card-content"]').text()).toBe('Body')
    expect(wrapper.find('[data-testid="data-card-footer"] [data-testid="card-footer"]').text()).toBe('Footer')
  })

  it('supports an explicit heading level while retaining the reusable default', () => {
    const defaultHeader = mount(CardHeader, { props: { title: 'Default heading' } })
    const customHeader = mount(CardHeader, { props: { title: 'Custom heading', headingLevel: 'h4' } })

    expect(defaultHeader.find('h3').text()).toBe('Default heading')
    expect(customHeader.find('h4').text()).toBe('Custom heading')
  })

  it('keeps SectionCard headings at h2', () => {
    const wrapper = mount(SectionCard, { props: { title: 'Settings' } })

    expect(wrapper.find('h2').text()).toBe('Settings')
    expect(wrapper.find('h3').exists()).toBe(false)
  })
})

describe('MetricCard', () => {
  it('renders delta values, direction, percentage, and custom test id', () => {
    const wrapper = mount(MetricCard, {
      props: { label: 'Runs', value: 12, delta: 2, deltaPct: 16.25, dataTestId: 'metric-card' },
    })

    expect(wrapper.attributes('data-testid')).toBe('metric-card')
    expect(wrapper.text()).toContain('+2 (16.3%)')
    expect(wrapper.text()).toContain('▲')
    expect(wrapper.find('.text-success-text').exists()).toBe(true)
  })

  it('renders progress when delta is absent and clamps the visual width', () => {
    const wrapper = mount(MetricCard, {
      props: { label: 'Capacity', value: 'Full', progress: { value: 120, max: 100 } },
    })

    expect(wrapper.find('.bg-destructive').exists()).toBe(true)
    expect(wrapper.find('[style="width: 100%;"]').exists()).toBe(true)
  })
})

describe('SummaryCard', () => {
  it('binds the configured grid test id and renders summary items', () => {
    const wrapper = mount(SummaryCard, {
      props: {
        title: 'Summary',
        items: [{ label: 'Passed', value: 4, testId: 'passed-item' }],
        gridTestId: 'custom-summary-grid',
      },
    })

    expect(wrapper.find('[data-testid="custom-summary-grid"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="risk-assessment-summary-grid"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="passed-item"]').text()).toContain('4')
  })
})
