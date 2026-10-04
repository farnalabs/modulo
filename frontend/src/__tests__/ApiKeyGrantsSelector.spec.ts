import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ApiKeyGrantsSelector from '../components/settings/ApiKeyGrantsSelector.vue'

const permissions = [
  { name: 'pipeline.create', min_role: 'operator' },
  { name: 'pipeline.list', min_role: 'viewer' },
  { name: 'run.trigger', min_role: 'runner' },
]

function mountSelector(props: Record<string, unknown> = {}) {
  return mount(ApiKeyGrantsSelector, {
    props: { permissions, restricted: false, selected: [], ...props },
  })
}

describe('ApiKeyGrantsSelector', () => {
  it('hides the permission list by default (role bundle)', () => {
    const wrapper = mountSelector()
    expect(wrapper.find('[data-testid="api-key-grants-list"]').exists()).toBe(false)
  })

  it('shows backend-supplied permissions grouped by prefix when restricted', () => {
    const wrapper = mountSelector({ restricted: true })
    expect(wrapper.find('[data-testid="api-key-grants-list"]').exists()).toBe(true)
    expect(wrapper.findAll('input[type="checkbox"]').length).toBe(1 + permissions.length)
    expect(wrapper.text()).toContain('Pipelines')
    expect(wrapper.text()).toContain('Runs')
  })

  it('emits restricted when the toggle is clicked', async () => {
    const wrapper = mountSelector()
    await wrapper.find('[data-testid="api-key-grants-restrict"]').setValue(true)
    expect(wrapper.emitted('update:restricted')?.[0]).toEqual([true])
  })

  it('emits the selection when a permission is ticked', async () => {
    const wrapper = mountSelector({ restricted: true })
    await wrapper.find('[data-testid="api-key-grant-run.trigger"]').setValue(true)
    expect(wrapper.emitted('update:selected')?.[0]).toEqual([['run.trigger']])
  })

  it('removes a permission when it is unticked', async () => {
    const wrapper = mountSelector({ restricted: true, selected: ['run.trigger', 'pipeline.list'] })
    await wrapper.find('[data-testid="api-key-grant-run.trigger"]').setValue(false)
    expect(wrapper.emitted('update:selected')?.[0]).toEqual([['pipeline.list']])
  })

  it('warns when restricted with nothing selected', () => {
    const wrapper = mountSelector({ restricted: true })
    expect(wrapper.find('[data-testid="api-key-grants-empty-error"]').exists()).toBe(true)
  })

  it('does not warn once something is selected', () => {
    const wrapper = mountSelector({ restricted: true, selected: ['run.trigger'] })
    expect(wrapper.find('[data-testid="api-key-grants-empty-error"]').exists()).toBe(false)
  })
})
