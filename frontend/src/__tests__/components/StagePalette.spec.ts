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

  it('sets the copy effect and stage type for every palette item', async () => {
    const wrapper = mount(StagePalette)
    const buttons = wrapper.findAll('button')
    const types = ['modulo', 'external', 'manual', 'placeholder']

    for (const [index, type] of types.entries()) {
      const dataTransfer = {
        setData: vi.fn(),
        effectAllowed: '',
      } as unknown as DataTransfer
      await buttons[index].trigger('dragstart', { dataTransfer })
      expect(dataTransfer.setData).toHaveBeenCalledWith(
        'application/lifecycle-stage',
        type,
      )
      expect(dataTransfer.effectAllowed).toBe('copy')
    }
  })

  it('leaves a drag without dataTransfer untouched', async () => {
    const wrapper = mount(StagePalette)
    await wrapper.find('button').trigger('dragstart', { dataTransfer: null })
    expect(wrapper.exists()).toBe(true)
  })
})
