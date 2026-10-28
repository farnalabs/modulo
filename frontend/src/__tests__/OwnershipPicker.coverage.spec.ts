import { describe, expect, it, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const getMock = vi.fn()

vi.mock('../lib/api/client', () => ({
  api: { GET: (...args: unknown[]) => getMock(...args) },
}))

import OwnershipPicker from '../components/OwnershipPicker.vue'

const teams = [
  { id: 't1', name: 'Team One', member_count: 1 },
  { id: 't2', name: 'Team Two', member_count: 3 },
]

const PopoverStub = {
  template: '<div><slot /></div>',
  methods: { toggle: vi.fn(), hide: vi.fn() },
}

function mountPicker(props: Record<string, unknown> = {}) {
  return mount(OwnershipPicker, {
    props,
    global: {
      stubs: { Popover: PopoverStub },
      mocks: { $t: (key: string) => key },
    },
  })
}

describe('OwnershipPicker behavior', () => {
  beforeEach(() => {
    getMock.mockReset()
    getMock.mockResolvedValue({ data: { items: teams }, error: undefined })
  })

  it('loads teams, renders labels, and emits org and team selections', async () => {
    const wrapper = mountPicker({ label: 'Owner', modelValue: { owner_team_id: 't1', visibility: 'team' } })
    await flushPromises()

    expect(wrapper.find('span').text()).toContain('Owner')
    expect(wrapper.text()).toContain('Team One')
    expect(wrapper.text()).toContain('1')
    expect(wrapper.text()).toContain('3')

    const buttons = wrapper.findAll('button')
    await buttons.find((button) => button.text().includes('orgwide'))?.trigger('click')
    await buttons.find((button) => button.text().includes('Team Two'))?.trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([
      [{ owner_team_id: null, visibility: 'org' }],
      [{ owner_team_id: 't2', visibility: 'team' }],
    ])
  })

  it('shows unknown team, loading, API errors, and thrown errors', async () => {
    const unknown = mountPicker({ modelValue: { owner_team_id: 'missing', visibility: 'team' } })
    await flushPromises()
    expect(unknown.text()).toContain('Unknown team')

    let resolveRequest!: (value: unknown) => void
    getMock.mockReturnValueOnce(new Promise((resolve) => { resolveRequest = resolve }))
    const loading = mountPicker()
    expect(loading.text()).toContain('loading_teams')
    resolveRequest({ data: { items: [] }, error: undefined })
    await flushPromises()

    getMock.mockResolvedValueOnce({ data: undefined, error: { detail: 'no teams' } })
    const apiError = mountPicker()
    await flushPromises()
    expect(apiError.text()).toContain('Failed to load teams')

    getMock.mockRejectedValueOnce(new Error('network down'))
    const thrownError = mountPicker()
    await flushPromises()
    expect(thrownError.text()).toContain('network down')
  })

  it('toggles the popover and renders the empty-team state', async () => {
    getMock.mockResolvedValue({ data: { items: [] }, error: undefined })
    const wrapper = mountPicker()
    await flushPromises()
    const trigger = wrapper.find('button[aria-haspopup="dialog"]')
    await trigger.trigger('click')
    expect(trigger.attributes('aria-expanded')).toBe('true')
    await trigger.trigger('click')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('hr').exists()).toBe(false)
  })
})
