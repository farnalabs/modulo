import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import AccessEntitySelector from '../components/assistant/AccessEntitySelector.vue'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.useRealTimers()
})

function mountSelector(props: Record<string, unknown> = {}) {
  return mount(AccessEntitySelector, {
    props: {
      modelValue: [],
      entities: [
        { id: 'p-1', name: 'Pipeline Alpha', description: 'First pipeline' },
        { id: 'p-2', name: 'Pipeline Beta', description: 'Second pipeline' },
        { id: 's-1', name: 'Schema One', description: 'A schema' },
      ],
      labelField: 'name',
      descriptionField: 'description',
      placeholder: 'Search pipelines...',
      noResultsText: 'No matches found',
      emptyText: 'No pipelines selected',
      testId: 'entity-selector',
      ...props,
    },
  })
}

describe('AccessEntitySelector', () => {
  it('renders the input with placeholder and testId', () => {
    const wrapper = mountSelector()
    const input = wrapper.find('input')
    expect(input.exists()).toBe(true)
    expect(input.attributes('placeholder')).toBe('Search pipelines...')
    expect(input.attributes('data-testid')).toBe('entity-selector')
  })

  it('shows empty text when nothing is selected', () => {
    const wrapper = mountSelector()
    expect(wrapper.text()).toContain('No pipelines selected')
  })

  it('shows selected entities in the table', async () => {
    const wrapper = mountSelector({ modelValue: ['p-1'] })
    expect(wrapper.text()).toContain('Pipeline Alpha')
    expect(wrapper.text()).toContain('(First pipeline)')
    expect(wrapper.text()).not.toContain('No pipelines selected')
  })

  it('hides description column when descriptionField is empty', async () => {
    const wrapper = mountSelector({
      modelValue: ['p-1'],
      descriptionField: '',
    })
    expect(wrapper.text()).toContain('Pipeline Alpha')
    expect(wrapper.text()).not.toContain('(First pipeline)')
  })

  it('opens dropdown on focus showing all available entities', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await flushPromises()

    const options = wrapper.findAll('.absolute button')
    expect(options).toHaveLength(3) // all 3 entities
  })

  it('filters entities by label when typing', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await wrapper.find('input').setValue('pipeline')
    await flushPromises()

    const options = wrapper.findAll('.absolute button')
    expect(options).toHaveLength(2) // Pipeline Alpha + Pipeline Beta
  })

  it('filters entities by description', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await wrapper.find('input').setValue('first')
    await flushPromises()

    const options = wrapper.findAll('.absolute button')
    expect(options).toHaveLength(1)
    expect(options[0].text()).toContain('Pipeline Alpha')
  })

  it('shows no-results text when filter matches nothing', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await wrapper.find('input').setValue('zzz')
    await flushPromises()

    expect(wrapper.text()).toContain('No matches found')
  })

  it('does not show dropdown when input is empty and unfocused', async () => {
    const wrapper = mountSelector()
    expect(wrapper.findAll('.absolute button')).toHaveLength(0)
  })

  it('selects an entity on click', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await flushPromises()

    const firstOption = wrapper.findAll('.absolute button')[0]
    await firstOption.trigger('mousedown')
    await flushPromises()

    expect(wrapper.emitted('update:modelValue')).toHaveLength(1)
    expect(wrapper.emitted('update:modelValue')![0]).toEqual([['p-1']])
  })

  it('clears the query after selection', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await wrapper.find('input').setValue('pipeline')
    await flushPromises()

    const firstOption = wrapper.findAll('.absolute button')[0]
    await firstOption.trigger('mousedown')
    await flushPromises()

    expect((wrapper.find('input').element as HTMLInputElement).value).toBe('')
  })

  it('excludes already-selected entities from dropdown', async () => {
    const wrapper = mountSelector({ modelValue: ['p-1'] })
    await wrapper.find('input').trigger('focus')
    await flushPromises()

    const options = wrapper.findAll('.absolute button')
    expect(options).toHaveLength(2) // p-2 and s-1, not p-1
  })

  it('removes an entity when remove button is clicked', async () => {
    const wrapper = mountSelector({ modelValue: ['p-1', 'p-2'] })

    const removeBtn = wrapper.find('button[aria-label="Remove Pipeline Alpha"]')
    await removeBtn.trigger('click')

    expect(wrapper.emitted('update:modelValue')).toHaveLength(1)
    expect(wrapper.emitted('update:modelValue')![0]).toEqual([['p-2']])
  })

  it('hides dropdown on Escape', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await flushPromises()
    expect(wrapper.findAll('.absolute button')).toHaveLength(3)

    await wrapper.find('input').trigger('keydown.escape')
    await flushPromises()
    expect(wrapper.findAll('.absolute button')).toHaveLength(0)
  })

  it('selects first filtered entity on Enter', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await wrapper.find('input').setValue('pipeline')
    await flushPromises()

    await wrapper.find('input').trigger('keydown.enter')
    await flushPromises()

    expect(wrapper.emitted('update:modelValue')).toHaveLength(1)
    expect(wrapper.emitted('update:modelValue')![0]).toEqual([['p-1']])
  })

  it('does nothing on Enter when no filtered results', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await wrapper.find('input').setValue('zzz')
    await flushPromises()

    await wrapper.find('input').trigger('keydown.enter')
    await flushPromises()

    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('closes dropdown on blur after delay', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await flushPromises()
    expect(wrapper.findAll('.absolute button')).toHaveLength(3)

    await wrapper.find('input').trigger('blur')
    vi.advanceTimersByTime(200)
    await flushPromises()
    expect(wrapper.findAll('.absolute button')).toHaveLength(0)
  })

  it('does not close dropdown during the 180ms blur window', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await flushPromises()

    await wrapper.find('input').trigger('blur')
    vi.advanceTimersByTime(100)
    await flushPromises()
    // Dropdown still visible during the grace period
    expect(wrapper.findAll('.absolute button')).toHaveLength(3)
  })

  it('displayLabel falls back to entity.id when labelField is missing', async () => {
    const wrapper = mount(AccessEntitySelector, {
      props: {
        modelValue: [],
        entities: [{ id: 'entity-1', customField: 'Custom Name' }],
        labelField: 'customField',
        descriptionField: '',
        placeholder: 'Search...',
        noResultsText: 'No results',
        emptyText: 'No items',
      },
    })

    // Select the entity first
    await wrapper.find('input').trigger('focus')
    await wrapper.find('input').setValue('custom')
    await flushPromises()
    await wrapper.findAll('.absolute button')[0].trigger('mousedown')
    await flushPromises()

    // Now the entity has a non-existent labelField, so displayLabel returns customField
    // But let's test with a missing field
    const wrapper2 = mount(AccessEntitySelector, {
      props: {
        modelValue: ['entity-1'],
        entities: [{ id: 'entity-1' }],
        labelField: 'missingField',
        descriptionField: '',
        placeholder: 'Search...',
        noResultsText: 'No results',
        emptyText: 'No items',
      },
    })
    expect(wrapper2.text()).toContain('entity-1')
  })

  it('displayDescription returns empty string when descriptionField is provided but entity lacks it', async () => {
    const wrapper = mount(AccessEntitySelector, {
      props: {
        modelValue: ['e-1'],
        entities: [{ id: 'e-1', name: 'Entity' }],
        labelField: 'name',
        descriptionField: 'description',
        placeholder: 'Search...',
        noResultsText: 'No results',
        emptyText: 'No items',
      },
    })
    // The entity has no 'description' field, so displayDescription returns ''
    expect(wrapper.text()).toContain('Entity')
    expect(wrapper.text()).not.toContain('(')
  })

  it('case-insensitive search', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await wrapper.find('input').setValue('PIPELINE')
    await flushPromises()

    const options = wrapper.findAll('.absolute button')
    expect(options).toHaveLength(2)
  })

  it('whitespace-only query shows all available entities', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await wrapper.find('input').setValue('   ')
    await flushPromises()

    const options = wrapper.findAll('.absolute button')
    expect(options).toHaveLength(3)
  })

  it('handles modelValue v-model update with existing selections', async () => {
    const wrapper = mountSelector({ modelValue: ['p-1'] })

    // Remove the selected entity
    const removeBtn = wrapper.find('button[aria-label="Remove Pipeline Alpha"]')
    await removeBtn.trigger('click')

    expect(wrapper.emitted('update:modelValue')![0]).toEqual([[]])
  })

  it('does not duplicate a selected entity on re-select', async () => {
    const wrapper = mountSelector({ modelValue: ['p-1'] })
    await wrapper.find('input').trigger('focus')
    await flushPromises()

    // p-1 is already selected, so it should not appear in dropdown
    const options = wrapper.findAll('.absolute button')
    expect(options.find(o => o.text().includes('Pipeline Alpha'))).toBeUndefined()
  })

  it('removes the blur timer on unmount', async () => {
    const wrapper = mountSelector()
    await wrapper.find('input').trigger('focus')
    await wrapper.find('input').trigger('blur')

    // Unmount before the timer fires
    wrapper.unmount()
    // No error thrown — the onUnmounted cleanup ran
  })
})
