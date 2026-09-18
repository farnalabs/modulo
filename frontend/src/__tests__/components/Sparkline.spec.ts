import { describe, it, expect } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import Sparkline from '../../components/shared/Sparkline.vue'

type Wrapper = ReturnType<typeof mount>

// jsdom exposes clientX/clientY as getter-only on MouseEvent, so @vue/test-utils
// trigger() cannot set them. Dispatch a real event with the coords defined and
// await the reactive DOM update.
// pointermove/pointerleave listeners live on the inner plot div (wrapping just
// the svg + tooltip), not the component root — the root also contains the
// y-axis label column, which must not affect pointer/tooltip math.
function plotElement(wrapper: Wrapper): Element {
  return wrapper.get('[data-testid="sparkline-plot"]').element
}

async function movePointer(wrapper: Wrapper, clientX: number, clientY: number) {
  const event = new Event('pointermove', { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'clientX', { value: clientX, configurable: true })
  Object.defineProperty(event, 'clientY', { value: clientY, configurable: true })
  plotElement(wrapper).dispatchEvent(event)
  await nextTick()
}

async function leavePointer(wrapper: Wrapper) {
  plotElement(wrapper).dispatchEvent(new Event('pointerleave', { bubbles: true }))
  await nextTick()
}

describe('Sparkline', () => {
  it('renders a polyline when there are at least two data points', () => {
    const wrapper = mount(Sparkline, { props: { data: [1, 4, 2, 8] } })
    const polyline = wrapper.find('polyline')
    expect(polyline.exists()).toBe(true)
    const ys = polyline.attributes('points')!
      .split(' ')
      .map(p => Number(p.split(',')[1]))
    // Every y must stay inside the plot area (2..58 for a 60-tall viewBox).
    ys.forEach(y => {
      expect(y).toBeGreaterThanOrEqual(2)
      expect(y).toBeLessThanOrEqual(58)
    })
  })

  it('centers a flat series in the vertical middle instead of pinning it to an edge', () => {
    const wrapper = mount(Sparkline, { props: { data: [5, 5, 5, 5], height: 40 } })
    const ys = wrapper
      .find('polyline')
      .attributes('points')!
      .split(' ')
      .map(p => Number(p.split(',')[1]))
    ys.forEach(y => expect(y).toBe(20)) // height / 2
  })

  it('centers an all-zero flat series in the vertical middle', () => {
    const wrapper = mount(Sparkline, { props: { data: [0, 0, 0] } })
    const ys = wrapper
      .find('polyline')
      .attributes('points')!
      .split(' ')
      .map(p => Number(p.split(',')[1]))
    ys.forEach(y => expect(y).toBe(30)) // height / 2 (default height 60)
  })

  it('shows a no-data placeholder instead of a misleading line for empty data', () => {
    const wrapper = mount(Sparkline, { props: { data: [] } })
    expect(wrapper.find('polyline').exists()).toBe(false)
    expect(wrapper.find('.sparkline-no-data').exists()).toBe(true)
    expect(wrapper.text()).toContain('No data')
  })

  it('shows a no-data placeholder for a single data point', () => {
    const wrapper = mount(Sparkline, { props: { data: [3] } })
    expect(wrapper.find('polyline').exists()).toBe(false)
    expect(wrapper.find('.sparkline-no-data').exists()).toBe(true)
  })

  it('exposes an accessible aria-label describing the series', () => {
    const wrapper = mount(Sparkline, { props: { data: [1, 2, 3], unit: 'runs' } })
    const svg = wrapper.find('svg')
    expect(svg.attributes('role')).toBe('img')
    expect(svg.attributes('aria-label')).toContain('Chart with 3 data points')
    expect(svg.attributes('aria-label')).toContain('1 runs')
    expect(svg.attributes('aria-label')).toContain('3 runs')
  })

  it('uses the localized no-data string as the aria-label when empty', () => {
    const wrapper = mount(Sparkline, { props: { data: [] } })
    expect(wrapper.find('svg').attributes('aria-label')).toBe('No data')
  })

  it('shows a tooltip with label, value and unit on pointermove', async () => {
    const wrapper = mount(Sparkline, {
      props: { data: [1, 2, 3], labels: ['Mon', 'Tue', 'Wed'], unit: 'runs', width: 200 },
    })
    expect(wrapper.find('[data-testid="sparkline-tooltip"]').exists()).toBe(false)
    await movePointer(wrapper, 199, 10)
    const tooltip = wrapper.find('[data-testid="sparkline-tooltip"]')
    expect(tooltip.exists()).toBe(true)
    expect(tooltip.text()).toContain('Wed')
    expect(tooltip.text()).toContain('3 runs')
  })

  it('formats the tooltip for dollar units', async () => {
    const wrapper = mount(Sparkline, {
      props: { data: [12.5, 15.2], unit: '$', width: 200 },
    })
    await movePointer(wrapper, 199, 10)
    const tooltip = wrapper.find('[data-testid="sparkline-tooltip"]')
    expect(tooltip.text()).toContain('$15.2')
  })

  it('hides the tooltip on pointerleave', async () => {
    const wrapper = mount(Sparkline, { props: { data: [1, 2, 3], labels: ['a', 'b', 'c'] } })
    await movePointer(wrapper, 199, 10)
    expect(wrapper.find('[data-testid="sparkline-tooltip"]').exists()).toBe(true)
    await leavePointer(wrapper)
    expect(wrapper.find('[data-testid="sparkline-tooltip"]').exists()).toBe(false)
  })

  it('renders y-axis labels when showYAxis is enabled and omits them otherwise', () => {
    const withAxis = mount(Sparkline, { props: { data: [1, 4, 2, 8], showYAxis: true } })
    const labels = withAxis.findAll('.sparkline-y-label')
    expect(labels).toHaveLength(3) // default tickCount
    // Y-axis labels now use integer formatting by default (no decimals).
    // Ticks for [1,4,2,8]: min=1, max=8, range=7, 3 ticks -> 1, 4.5, 8 -> displayed as 1, 5, 8.
    const texts = labels.map(l => l.text()).sort()
    expect(texts).toEqual(['1', '5', '8'])
    // Labels are plain HTML <span> elements outside the svg (never subject to
    // its non-uniform preserveAspectRatio="none" scale), laid out top-to-bottom
    // via flexbox — the highest value renders first.
    labels.forEach(l => expect(l.element.tagName).toBe('SPAN'))
    expect(labels[0]!.text()).toBe('8')
    expect(labels[labels.length - 1]!.text()).toBe('1')

    const withoutAxis = mount(Sparkline, { props: { data: [1, 4, 2, 8] } })
    expect(withoutAxis.findAll('.sparkline-y-label')).toHaveLength(0)
  })

  it('honours the tickCount floor of 2 y-axis labels', () => {
    const wrapper = mount(Sparkline, {
      props: { data: [1, 4, 2, 8], showYAxis: true, tickCount: 0 },
    })
    expect(wrapper.findAll('.sparkline-y-label')).toHaveLength(2)
  })

  it('formats count y-axis labels as integers (no decimals)', () => {
    const wrapper = mount(Sparkline, {
      props: { data: [10, 20, 30, 40], showYAxis: true },
    })
    const labels = wrapper.findAll('.sparkline-y-label')
    const texts = labels.map(l => l.text())
    // All labels should be integers — no decimal points.
    texts.forEach(t => expect(t).not.toContain('.'))
  })

  it('formats percent y-axis labels as integers', () => {
    const wrapper = mount(Sparkline, {
      props: { data: [72.5, 85.3, 91.1, 78.9], unit: '%', showYAxis: true },
    })
    const labels = wrapper.findAll('.sparkline-y-label')
    const texts = labels.map(l => l.text())
    // Percent labels should be integers — no decimal points.
    texts.forEach(t => expect(t).not.toContain('.'))
  })

  it('formats dollar y-axis labels as integers for normal spend', () => {
    const wrapper = mount(Sparkline, {
      props: { data: [12.5, 15.2, 18.9, 22.1], unit: '$', showYAxis: true },
    })
    const labels = wrapper.findAll('.sparkline-y-label')
    const texts = labels.map(l => l.text())
    // Dollar labels should be integer with $ prefix — no decimal points.
    texts.forEach(t => {
      expect(t.startsWith('$')).toBe(true)
      // After the $, should be integer (no decimal point).
      expect(t.slice(1)).not.toContain('.')
    })
  })

  it('preserves sub-dollar precision for small spend series', () => {
    const wrapper = mount(Sparkline, {
      props: { data: [0.03, 0.07, 0.12, 0.05], unit: '$', showYAxis: true },
    })
    const labels = wrapper.findAll('.sparkline-y-label')
    const texts = labels.map(l => l.text())
    // Small dollar values should keep decimal precision so labels are not all $0.
    // At least one label should contain a decimal point.
    const hasDecimal = texts.some(t => t.slice(1).includes('.'))
    expect(hasDecimal).toBe(true)
    // No label should be "$0" — that would be a useless axis.
    texts.forEach(t => expect(t).not.toBe('$0'))
  })

  it('bumps precision when integer dollar labels all collapse to the same value', () => {
    // Every tick rounds to the same integer ($1) — collapse detection must add
    // 1 decimal so the axis stays informative.
    const wrapper = mount(Sparkline, {
      props: { data: [1.1, 1.2, 1.3, 1.4], unit: '$', showYAxis: true },
    })
    const labels = wrapper.findAll('.sparkline-y-label')
    const texts = labels.map(l => l.text())
    // With collapse detection: at least two distinct labels should exist.
    const unique = new Set(texts)
    expect(unique.size).toBeGreaterThan(1)
    // The bumped labels keep the $ prefix and a single decimal place.
    texts.forEach(t => {
      expect(t.startsWith('$')).toBe(true)
      expect(t.slice(1)).toContain('.')
    })
  })

  it('bumps precision when integer count labels all collapse to the same value', () => {
    // Every tick rounds to 0 as an integer — collapse detection must add 1 decimal.
    const wrapper = mount(Sparkline, {
      props: { data: [0.1, 0.2, 0.3, 0.4], showYAxis: true },
    })
    const labels = wrapper.findAll('.sparkline-y-label')
    const texts = labels.map(l => l.text())
    // With collapse detection: at least two distinct labels should exist.
    const unique = new Set(texts)
    expect(unique.size).toBeGreaterThan(1)
    // The bumped labels carry a single decimal place.
    texts.forEach(t => expect(t).toContain('.'))
  })

  it('bumps precision when integer percent labels all collapse to the same value', () => {
    // Every tick rounds to 50% as an integer — collapse detection must add 1 decimal.
    const wrapper = mount(Sparkline, {
      props: { data: [50.1, 50.2, 50.3, 50.4], unit: '%', showYAxis: true },
    })
    const labels = wrapper.findAll('.sparkline-y-label')
    const texts = labels.map(l => l.text())
    // With collapse detection: at least two distinct labels should exist.
    const unique = new Set(texts)
    expect(unique.size).toBeGreaterThan(1)
    // The bumped labels carry a single decimal place (the % unit is rendered
    // separately from the numeric label, so the label itself has no suffix).
    texts.forEach(t => expect(t).toContain('.'))
  })

  it('bumps precision when sub-dollar labels all collapse to $0.00', () => {
    // Every tick rounds to $0.00 at 2-decimal precision — collapse detection must
    // add a 3rd decimal so the axis stays informative.
    const wrapper = mount(Sparkline, {
      props: { data: [0.001, 0.002, 0.003, 0.004], unit: '$', showYAxis: true },
    })
    const labels = wrapper.findAll('.sparkline-y-label')
    const texts = labels.map(l => l.text())
    // With collapse detection: at least two distinct labels should exist.
    const unique = new Set(texts)
    expect(unique.size).toBeGreaterThan(1)
    // No label should be the useless "$0.00" collapse.
    texts.forEach(t => expect(t).not.toBe('$0.00'))
  })

  it('renders x-axis tick marks when showXTicks is enabled and omits them otherwise', () => {
    const withTicks = mount(Sparkline, { props: { data: [1, 4, 2, 8], showXTicks: true } })
    const ticks = withTicks.findAll('line.sparkline-x-tick')
    expect(ticks).toHaveLength(4) // <= 8 points render a tick at every index
    ticks.forEach(t => {
      const y2 = Number(t.attributes('y2'))
      const y1 = Number(t.attributes('y1'))
      // Ticks must stay fully inside the viewBox (not clipped at the bottom edge).
      expect(y1).toBeLessThanOrEqual(60)
      expect(y2).toBeGreaterThanOrEqual(0)
      expect(y2).toBeLessThanOrEqual(60)
    })

    const withoutTicks = mount(Sparkline, { props: { data: [1, 4, 2, 8] } })
    expect(withoutTicks.findAll('line.sparkline-x-tick')).toHaveLength(0)
  })

  it('decimates x-axis ticks for more than 8 data points', () => {
    const data = Array.from({ length: 30 }, (_, i) => i + 1)
    const wrapper = mount(Sparkline, { props: { data, showXTicks: true, width: 200 } })
    // everyN = ceil(30 / 8) = 4 -> indices 0,4,8,12,16,20,24,28 plus the final index 29.
    expect(wrapper.findAll('line.sparkline-x-tick')).toHaveLength(9)
  })

  it('resolves the correct hovered data point when showYAxis is enabled', async () => {
    const data = Array.from({ length: 30 }, (_, i) => i + 1)
    const labels = data.map((_, i) => `day${i}`)
    const wrapper = mount(Sparkline, {
      props: { data, labels, width: 200, unit: 'runs', showYAxis: true },
    })
    // The y-axis label column lives outside the svg/plot div entirely (a
    // sibling flex item), so pointer math inside the plot always uses plain
    // width/padding regardless of showYAxis — no adjustment needed.
    await movePointer(wrapper, 2, 10)
    expect(wrapper.find('[data-testid="sparkline-tooltip"]').text()).toContain('day0')
    await movePointer(wrapper, 198, 10)
    expect(wrapper.find('[data-testid="sparkline-tooltip"]').text()).toContain('day29')
  })

  it('does not show a tooltip when there is no data', async () => {
    const wrapper = mount(Sparkline, { props: { data: [] } })
    await movePointer(wrapper, 100, 10)
    expect(wrapper.find('[data-testid="sparkline-tooltip"]').exists()).toBe(false)
  })

  it('zero mode (default) maps null values to 0 at the chart floor', () => {
    const wrapper = mount(Sparkline, { props: { data: [null, 50, null] } })
    const ys = wrapper
      .find('polyline')
      .attributes('points')!
      .split(' ')
      .map(p => Number(p.split(',')[1]))
    // [0, 50, 0]: the null days render as 0 (floor, y=58), the 50 day at the top.
    expect(ys).toHaveLength(3)
    expect(ys[0]).toBe(58)
    expect(ys[1]).toBe(2)
    expect(ys[2]).toBe(58)
  })

  it('carry-forward replaces interior and trailing nulls with the last known value', () => {
    const wrapper = mount(Sparkline, {
      props: { data: [80, null, 100, null], missingData: 'carry-forward' },
    })
    const ys = wrapper
      .find('polyline')
      .attributes('points')!
      .split(' ')
      .map(p => Number(p.split(',')[1]))
    expect(ys).toHaveLength(4)
    const expected = mount(Sparkline, { props: { data: [80, 80, 100, 100] } })
    const expectedYs = expected
      .find('polyline')
      .attributes('points')!
      .split(' ')
      .map(p => Number(p.split(',')[1]))
    expect(ys).toEqual(expectedYs)
  })

  it('carry-forward draws a straight line across null days instead of crashing to the floor', () => {
    const wrapper = mount(Sparkline, {
      props: { data: [100, null, null, 100, null], missingData: 'carry-forward' },
    })
    const ys = wrapper
      .find('polyline')
      .attributes('points')!
      .split(' ')
      .map(p => Number(p.split(',')[1]))
    // Flat series at 100 -> centred vertically (height 60), one point per raw day.
    expect(ys).toHaveLength(5)
    ys.forEach(y => expect(y).toBe(30))
  })

  it('carry-forward drops leading nulls so the line starts at the first day with data', () => {
    const wrapper = mount(Sparkline, {
      props: { data: [null, null, 80, 100, null], missingData: 'carry-forward' },
    })
    const ys = wrapper
      .find('polyline')
      .attributes('points')!
      .split(' ')
      .map(p => Number(p.split(',')[1]))
    // Transformed series [80, 100, 100]: leading nulls are gone, the trailing
    // null carries 100 forward. 80 sits on the floor (58), 100 at the top (2).
    expect(ys).toEqual([58, 2, 2])
  })

  it('keeps hover tooltips date-aligned after dropping leading nulls', async () => {
    const wrapper = mount(Sparkline, {
      props: {
        data: [null, null, 80, 100, null],
        labels: ['d0', 'd1', 'd2', 'd3', 'd4'],
        missingData: 'carry-forward',
        width: 200,
      },
    })
    await movePointer(wrapper, 2, 10)
    let tooltip = wrapper.find('[data-testid="sparkline-tooltip"]')
    expect(tooltip.text()).toContain('d2')
    expect(tooltip.text()).toContain('80')
    await movePointer(wrapper, 198, 10)
    tooltip = wrapper.find('[data-testid="sparkline-tooltip"]')
    expect(tooltip.text()).toContain('d4')
    expect(tooltip.text()).toContain('100')
  })

  it('renders the no-data placeholder when every value is null', () => {
    const wrapper = mount(Sparkline, {
      props: { data: [null, null, null], missingData: 'carry-forward' },
    })
    expect(wrapper.find('polyline').exists()).toBe(false)
    expect(wrapper.find('.sparkline-no-data').exists()).toBe(true)
  })

  it('renders a uniform scaling-independent endpoint dot', () => {
    const wrapper = mount(Sparkline, { props: { data: [1, 4, 2, 8] } })
    const dot = wrapper.find('[data-testid="sparkline-endpoint"]')
    expect(dot.exists()).toBe(true)
    // A <circle> would be squashed into a flat dash by the non-uniform svg
    // scale; the dot must be a zero-length path with a round, non-scaling stroke.
    expect(wrapper.find('circle[data-testid="sparkline-endpoint"]').exists()).toBe(false)
    expect(dot.element.tagName.toLowerCase()).toBe('path')
    expect(dot.attributes('vector-effect')).toBe('non-scaling-stroke')
    expect(dot.attributes('stroke-linecap')).toBe('round')
    expect(dot.attributes('stroke-width')).toBe('5')
    expect(dot.attributes('fill')).toBe('none')
  })
})
