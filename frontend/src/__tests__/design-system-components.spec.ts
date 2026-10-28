import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import BaseCard from '../components/ui/BaseCard.vue'
import CardHeader from '../components/ui/CardHeader.vue'
import DataCard from '../components/ui/DataCard.vue'
import MetricCard from '../components/ui/MetricCard.vue'
import SummaryCard from '../components/ui/SummaryCard.vue'
import SectionCard from '../components/shared/SectionCard.vue'
import StatCard from '../components/StatCard.vue'

describe('BaseCard', () => {
  it.each([
    ['default', undefined, 'bg-card'],
    ['bordered', 'bordered', 'border'],
    ['elevated', 'elevated', 'shadow'],
    ['muted', 'muted', 'bg-muted/50'],
  ] as const)('maps the %s variant', (_name, variant, expectedClass) => {
    const wrapper = mount(BaseCard, { props: { variant } })

    expect(wrapper.classes()).toContain(expectedClass)
  })

  it.each([
    ['none', 'none', 'p-0'],
    ['sm', 'sm', 'p-3'],
    ['md', 'md', 'p-4'],
    ['lg', 'lg', 'p-6'],
    ['default', undefined, 'p-4'],
  ] as const)('maps %s padding', (_name, padding, expectedClass) => {
    const wrapper = mount(BaseCard, { props: { padding } })

    expect(wrapper.classes()).toContain(expectedClass)
  })

  it.each([
    ['none', 'none', ''],
    ['sm', 'sm', 'shadow-sm'],
    ['md', 'md', 'shadow'],
    ['lg', 'lg', 'shadow-lg'],
    ['default', undefined, ''],
  ] as const)('maps %s elevation', (_name, elevation, expectedClass) => {
    const wrapper = mount(BaseCard, { props: { elevation } })

    if (expectedClass) {
      expect(wrapper.classes()).toContain(expectedClass)
    } else {
      expect(wrapper.classes()).not.toContain('shadow-sm')
      expect(wrapper.classes()).not.toContain('shadow-lg')
    }
  })

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
    expect(wrapper.classes()).toEqual(expect.arrayContaining(['card', 'card-base', 'bg-card', 'border', 'p-6', 'shadow-sm', 'custom-card']))
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

  it('renders description-only and action-only headers', () => {
    const descriptionOnly = mount(CardHeader, { props: { description: 'Context' } })
    const actionOnly = mount(CardHeader, { slots: { actions: '<button>Action</button>' } })

    expect(descriptionOnly.find('p').text()).toBe('Context')
    expect(descriptionOnly.find('h2, h3, h4').exists()).toBe(false)
    expect(actionOnly.find('button').text()).toBe('Action')
    expect(actionOnly.find('.card-header').exists()).toBe(true)
  })

   it('does not render an empty header for a title-less, action-less DataCard', () => {
     const wrapper = mount(DataCard, { slots: { default: '<p>Body</p>' } })

     expect(wrapper.find('.card-header').exists()).toBe(false)
     expect(wrapper.find('.card-content').text()).toBe('Body')
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
    expect(wrapper.text()).toContain('+2 (+16.3%)')
    expect(wrapper.text()).toContain('▲')
   expect(wrapper.find('.text-success-text').exists()).toBe(true)
   })

    it.each([
      [{ delta: -2 }, '-2'],
      [{ delta: 2 }, '+2'],
      [{ deltaPct: 16.25 }, '+16.3%'],
      [{ delta: Number.NaN, deltaPct: Number.POSITIVE_INFINITY }, ''],
      [{ delta: 0, deltaPct: 0 }, '0 (0.0%)'],
      [{ delta: 2, deltaPct: -16.25 }, '+2 (-16.3%)'],
    ])('renders valid delta combinations safely: %o', (deltaProps, expected) => {
      const wrapper = mount(MetricCard, {
        props: { label: 'Runs', value: 12, ...deltaProps },
      })

      if (expected) {
        expect(wrapper.text()).toContain(expected)
      } else {
        expect(wrapper.text()).not.toContain('NaN')
        expect(wrapper.text()).not.toContain('Infinity')
        expect(wrapper.find('.text-xs.font-medium').exists()).toBe(false)
      }
    })

    it.each([
      ['success', 'text-success-text'],
      ['warning', 'text-warning-text'],
      ['destructive', 'text-destructive'],
      ['muted', 'text-muted-foreground'],
      ['primary', ''],
    ] as const)('maps %s value color', (color, expectedClass) => {
      const wrapper = mount(MetricCard, { props: { label: 'Runs', value: 1, color } })

      if (expectedClass) expect(wrapper.find('.text-2xl').classes()).toContain(expectedClass)
      else expect(wrapper.find('.text-2xl').classes()).not.toContain('text-success-text')
    })

    it('preserves the absent test id when no custom id is provided', () => {
       expect(mount(MetricCard, { props: { label: 'Runs', value: 1 } }).attributes('data-testid')).toBeUndefined()
     })


   it.each([
     [{ value: 120, max: 100 }, '100%'],
     [{ value: -10, max: 100 }, '0%'],
     [{ value: 50, max: 0 }, '0%'],
     [{ value: 50, max: -10 }, '0%'],
   ])('clamps progress safely for %o', (progress, width) => {
     const wrapper = mount(MetricCard, {
       props: { label: 'Capacity', value: 'Full', progress },
     })

     expect(wrapper.find('[style]').attributes('style')).toContain(`width: ${width}`)
   })

    it('uses the destructive style for over-max progress', () => {
      const wrapper = mount(MetricCard, {
        props: { label: 'Capacity', value: 'Full', progress: { value: 120, max: 100 } },
      })

      expect(wrapper.find('.bg-destructive').exists()).toBe(true)
    })

    it('falls back to zero progress for non-finite values', () => {
      const invalidMax = mount(MetricCard, {
        props: { label: 'Capacity', value: 'Unknown', progress: { value: 50, max: Number.NaN } },
      })
      const invalidValue = mount(MetricCard, {
        props: { label: 'Capacity', value: 'Unknown', progress: { value: Number.NaN, max: 100 } },
      })

      expect(invalidMax.find('[style]').attributes('style')).toContain('width: 0%')
      expect(invalidValue.find('[style]').attributes('style')).toContain('width: 0%')
    })

    it.each([
      [{ value: 95, max: 100, color: 'primary' }, 'bg-warning'],
      [{ value: 80, max: 100, color: 'primary' }, 'bg-amber-500'],
      [{ value: 50, max: 100, color: 'success' }, 'bg-success'],
      [{ value: 50, max: 100, color: 'warning' }, 'bg-warning'],
      [{ value: 50, max: 100, color: 'destructive' }, 'bg-destructive'],
      [{ value: 50, max: 100, color: 'primary' }, 'bg-primary'],
    ] as const)('maps progress %o to %s', (progress, expectedClass) => {
      const wrapper = mount(MetricCard, { props: { label: 'Capacity', value: 'Half', progress } })

      expect(wrapper.find('[style]').classes()).toContain(expectedClass)
    })
 })

describe('StatCard', () => {
  it('binds a custom test id and preserves the default', () => {
    const custom = mount(StatCard, { props: { label: 'Runs', value: 12, dataTestId: 'custom-stat' } })
    const fallback = mount(StatCard, { props: { label: 'Runs', value: 12 } })

    expect(custom.find('[data-testid="custom-stat"]').exists()).toBe(true)
    expect(fallback.find('[data-testid="dashboard-stats-card"]').exists()).toBe(true)
  })

  it.each([
    ['primary', 'bg-primary/10', ''],
    ['success', 'bg-success/10', 'text-success-text'],
    ['warning', 'bg-warning/10', 'text-warning-text'],
    ['destructive', 'bg-destructive/10', 'text-destructive'],
    ['muted', 'bg-muted', 'text-muted-foreground'],
  ] as const)('maps %s color tokens', (color, iconClass, valueClass) => {
    const wrapper = mount(StatCard, { props: { label: 'Runs', value: 12, color } })

    expect(wrapper.find('svg').element.parentElement?.className).toContain(iconClass)
    if (valueClass) expect(wrapper.find('.text-2xl').classes()).toContain(valueClass)
  })

  it.each([
    [{ current: 12, previous: 10, delta_pct: 20 }, '▲', '+2 (20.0%)', 'text-success-text'],
    [{ current: 8, previous: 10, delta_pct: -20 }, '▼', '-2 (20.0%)', 'text-destructive'],
    [{ current: 10, previous: 10, delta_pct: 0 }, '→', '0 (0.0%)', 'text-muted-foreground'],
  ] as const)('renders percentage and absolute delta state %o', (delta, arrow, text, className) => {
    const wrapper = mount(StatCard, { props: { label: 'Runs', value: 10, delta } })

    expect(wrapper.text()).toContain(arrow)
    expect(wrapper.text()).toContain(text)
    expect(wrapper.find('.text-xs').classes()).toContain(className)
  })

  it('supports inverted and neutral delta styling plus no-baseline absolute deltas', () => {
    const inverted = mount(StatCard, {
      props: { label: 'Failures', value: 8, inverted: true, delta: { current: 8, previous: 10, delta_pct: -20 } },
    })
    const neutral = mount(StatCard, {
      props: { label: 'Runs', value: 12, neutralTrend: true, delta: { current: 12, previous: 10, delta_pct: 20 } },
    })
    const noBaseline = mount(StatCard, {
      props: { label: 'Runs', value: 12, delta: { current: 12, previous: 10 }, noBaselineLabel: 'No baseline' },
    })

    expect(inverted.find('.text-xs').classes()).toContain('text-success-text')
    expect(neutral.find('.text-xs').classes()).toContain('text-muted-foreground')
    expect(noBaseline.find('[data-testid="stat-no-baseline"]').text()).toContain('+2')
    expect(noBaseline.find('[data-testid="stat-no-baseline"]').attributes('title')).toBe('No baseline')
  })

  it('renders as a router link when a destination is provided', () => {
    const wrapper = mount(StatCard, { props: { label: 'Runs', value: 12, to: '/runs' } })

    expect(wrapper.find('a').attributes('href')).toBe('/runs')
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

    it('passes an explicit heading level through to CardHeader', () => {
      const wrapper = mount(SummaryCard, {
        props: {
          title: 'Summary',
          headingLevel: 'h2',
          items: [],
        },
      })

      expect(wrapper.find('h2').text()).toBe('Summary')
      expect(wrapper.find('h3').exists()).toBe(false)
    })

    it('renders all supported item colors, fallback color, footnote, header action, and footer', () => {
      const wrapper = mount(SummaryCard, {
        props: {
          items: [
            { label: 'Primary', value: 1, color: 'primary' },
            { label: 'Success', value: 2, color: 'success' },
            { label: 'Warning', value: 3, color: 'warning' },
            { label: 'Destructive', value: 4, color: 'destructive' },
            { label: 'Muted', value: 5, color: 'muted' },
            { label: 'Foreground', value: 6, color: 'foreground' },
            { label: 'Unknown', value: 7, color: 'unknown' as never },
          ],
          footnote: 'Updated recently',
          footnoteTestId: 'summary-footnote',
        },
        slots: {
          headerActions: '<button data-testid="summary-action">Action</button>',
          footer: '<span data-testid="summary-footer">Footer</span>',
        },
      })

      expect(wrapper.find('[data-testid="summary-footnote"]').text()).toBe('Updated recently')
      expect(wrapper.find('[data-testid="summary-action"]').text()).toBe('Action')
      expect(wrapper.find('[data-testid="summary-footer"]').text()).toBe('Footer')
      expect(wrapper.find('.text-primary-text').text()).toContain('1')
      expect(wrapper.find('.text-success-text').text()).toContain('2')
      expect(wrapper.find('.text-warning-text').text()).toContain('3')
      expect(wrapper.find('.text-destructive-text').text()).toContain('4')
      expect(wrapper.findAll('.text-muted-foreground').some((item) => item.text().includes('5'))).toBe(true)
      expect(wrapper.findAll('.text-foreground').some((item) => item.text().includes('6'))).toBe(true)
      expect(wrapper.findAll('.text-foreground').some((item) => item.text().includes('7'))).toBe(true)
    })
  })
