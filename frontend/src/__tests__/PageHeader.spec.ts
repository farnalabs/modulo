import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import PageHeader from '../components/shared/PageHeader.vue'

function mountPageHeader(
  props: { title: string; subtitle?: string; backLink?: string; dataTestId?: string },
  options: { rightSlot?: string; routerPush?: ReturnType<typeof vi.fn> } = {},
) {
  return mount(PageHeader, {
    props,
    slots: options.rightSlot ? { right: options.rightSlot } : {},
    global: { mocks: { $router: { push: options.routerPush ?? vi.fn() } } },
  })
}

describe('PageHeader', () => {
  it('renders the title and subtitle', () => {
    const wrapper = mountPageHeader({ title: 'Runs', subtitle: 'View all pipeline executions' })
    expect(wrapper.find('h1').text()).toBe('Runs')
    expect(wrapper.find('p').text()).toBe('View all pipeline executions')
  })

  it('renders no subtitle element when subtitle is absent', () => {
    const wrapper = mountPageHeader({ title: 'Runs' })
    expect(wrapper.find('h1').text()).toBe('Runs')
    expect(wrapper.find('p').exists()).toBe(false)
  })

  it('applies dataTestId to the header element', () => {
    const wrapper = mountPageHeader({ title: 'Runs', dataTestId: 'runs-title' })
    expect(wrapper.find('header[data-testid="runs-title"]').exists()).toBe(true)
  })

  it('renders no back button when backLink is absent', () => {
    const wrapper = mountPageHeader({ title: 'Runs' })
    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('navigates to the backLink when the back button is clicked', async () => {
    const routerPush = vi.fn()
    const wrapper = mountPageHeader({ title: 'Runs', backLink: '/pipelines' }, { routerPush })
    const backButton = wrapper.find('button')
    expect(backButton.exists()).toBe(true)
    await backButton.trigger('click')
    expect(routerPush).toHaveBeenCalledWith('/pipelines')
  })

  it('renders #right slot content', () => {
    const wrapper = mountPageHeader({ title: 'Runs' }, { rightSlot: '<span data-testid="right-slot-content">Filters</span>' })
    expect(wrapper.find('[data-testid="right-slot-content"]').exists()).toBe(true)
  })

  it('renders no right-slot container when no #right slot is provided', () => {
    const wrapper = mountPageHeader({ title: 'Runs' })
    expect(wrapper.findAll('header > div')).toHaveLength(1)
  })

  it('stacks title and actions vertically on mobile and rows them at sm+ (FAR-627)', () => {
    const wrapper = mountPageHeader({ title: 'Runs', subtitle: 'View all pipeline executions' })
    expect(wrapper.find('header').classes()).toEqual(
      expect.arrayContaining(['flex', 'flex-col', 'sm:flex-row', 'sm:items-start', 'sm:justify-between', 'gap-4']),
    )
  })

  it('makes the right-slot container full width and wrapping on mobile, intrinsic width at sm+ (FAR-627)', () => {
    const wrapper = mountPageHeader({ title: 'Runs' }, { rightSlot: '<span data-testid="right-slot-content">Filters</span>' })
    const rightContainer = wrapper.find('[data-testid="right-slot-content"]').element.parentElement
    expect(rightContainer).toBeTruthy()
    const classes = rightContainer!.className.split(' ')
    expect(classes).toEqual(expect.arrayContaining(['flex', 'flex-wrap', 'items-center', 'gap-2', 'w-full', 'sm:w-auto', 'sm:shrink-0']))
  })
})
