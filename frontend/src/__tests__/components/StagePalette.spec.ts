import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import StagePalette from '../../components/lifecycle-map/editor/StagePalette.vue'

describe('StagePalette', () => {
  it('renders one draggable button per stage type', () => {
    const wrapper = mount(StagePalette)
    const buttons = wrapper.findAll('button')
    expect(buttons).toHaveLength(4)
    buttons.forEach((b) => {
      expect(b.attributes('draggable')).toBe('true')
      expect(b.attributes('type')).toBe('button')
    })
  })

  it('emits a stage type via the dragstart dataTransfer payload', async () => {
    const wrapper = mount(StagePalette)
    const dataTransfer = {
      setData: vi.fn(),
      effectAllowed: '',
    } as unknown as DataTransfer
    await wrapper.findAll('button')[0].trigger('dragstart', { dataTransfer })
    expect(dataTransfer.setData).toHaveBeenCalledWith(
      'application/lifecycle-stage',
      'modulo',
    )
  })
})
