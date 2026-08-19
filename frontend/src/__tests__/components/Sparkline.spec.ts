import { describe, it, expect } from 'vitest'
import { nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import Sparkline from '../../components/shared/Sparkline.vue'

type Wrapper = ReturnType<typeof mount>

// jsdom exposes clientX/clientY as getter-only on MouseEvent, so @vue/test-utils
// trigger() cannot set them. Dispatch a real event with the coords defined and
// await the reactive DOM update.
async function movePointer(wrapper: Wrapper, clientX: number, clientY: number) {
  const event = new Event('pointermove', { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'clientX', { value: clientX, configurable: true })
  Object.defineProperty(event, 'clientY', { value: clientY, configurable: true })
  wrapper.element.dispatchEvent(event)
  await nextTick()
}

async function leavePointer(wrapper: Wrapper) {
  wrapper.element.dispatchEvent(new Event('pointerleave', { bubbles: true }))
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

  it('does not show a tooltip when there is no data', async () => {
    const wrapper = mount(Sparkline, { props: { data: [] } })
    await movePointer(wrapper, 100, 10)
    expect(wrapper.find('[data-testid="sparkline-tooltip"]').exists()).toBe(false)
  })
})
