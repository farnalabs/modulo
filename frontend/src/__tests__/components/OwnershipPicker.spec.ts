import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const getMock = vi.fn()

vi.mock('../../lib/api/client', () => ({
  api: {
    GET: (...args: unknown[]) => getMock(...args),
    POST: vi.fn(),
    PUT: vi.fn(),
    PATCH: vi.fn(),
    DELETE: vi.fn(),
  },
  getAccessToken: vi.fn(() => 'token'),
}))

import OwnershipPicker from '../../components/OwnershipPicker.vue'

const teams = [
  { id: 't1', name: 'Team One', member_count: 3 },
  { id: 't2', name: 'Team Two', member_count: 1 },
]

// PrimeVue's Popover only renders its slot content once it is open, so stub it
// to always render its default slot in tests.
const PopoverStub = {
  name: 'Popover',
  template: '<div class="popover-stub"><slot /></div>',
}

describe('OwnershipPicker', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    getMock.mockReset()
    getMock.mockResolvedValue({ data: { items: teams }, error: undefined })
  })

  it('renders the trigger with a populated team list and separator', async () => {
    const wrapper = mount(OwnershipPicker, {
      global: { stubs: { Popover: PopoverStub } },
    })
    await wrapper.vm.$nextTick()
    await new Promise((r) => setTimeout(r, 0))
    await wrapper.vm.$nextTick()
    expect(wrapper.find('button[aria-haspopup="dialog"]').exists()).toBe(true)
    // The separator (and team rows) only render once teams have loaded.
    expect(wrapper.findAll('hr').length).toBeGreaterThanOrEqual(1)
    expect(wrapper.text()).toContain('Team One')
  })
})
