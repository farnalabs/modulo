import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  }),
}))

import PortDefinitionPanel from '../components/pipeline/composite/PortDefinitionPanel.vue'
import type { ParameterPort } from '../types/pipeline'

function mountPanel() {
  return mount(PortDefinitionPanel, {
    props: { ports: [], nodeIds: [], nodes: [] },
  })
}

function lastPort(wrapper: ReturnType<typeof mountPanel>): ParameterPort {
  const emitted = wrapper.emitted('update:ports') as unknown[][]
  const ports = emitted[emitted.length - 1][0] as ParameterPort[]
  return ports[ports.length - 1]
}

describe('PortDefinitionPanel savePort default_value branches (SonarCloud coverage)', () => {
  it('leaves default_value undefined when the default is empty', async () => {
    const wrapper = mountPanel()
    const vm = wrapper.vm as any
    vm.form.name = 'eport'
    vm.form.label = 'E Port'
    vm.form.type = 'string'
    vm.form.default = ''
    vm.savePort()
    expect(lastPort(wrapper).default_value).toBeUndefined()
  })

  it('coerces to a number when type is number', async () => {
    const wrapper = mountPanel()
    const vm = wrapper.vm as any
    vm.form.name = 'nport'
    vm.form.label = 'N Port'
    vm.form.type = 'number'
    vm.form.default = '42'
    vm.savePort()
    expect(lastPort(wrapper).default_value).toBe(42)
  })

  it('sets boolean default_value when type is boolean', async () => {
    const wrapper = mountPanel()
    const vm = wrapper.vm as any
    vm.form.name = 'bport'
    vm.form.label = 'B Port'
    vm.form.type = 'boolean'
    vm.form.default = 'true'
    vm.savePort()
    expect(lastPort(wrapper).default_value).toBe(true)
  })

  it('keeps the raw string default_value when type is string', async () => {
    const wrapper = mountPanel()
    const vm = wrapper.vm as any
    vm.form.name = 'sport'
    vm.form.label = 'S Port'
    vm.form.type = 'string'
    vm.form.default = 'hello'
    vm.savePort()
    expect(lastPort(wrapper).default_value).toBe('hello')
  })

  it('keeps the raw string default_value when type is select', async () => {
    const wrapper = mountPanel()
    const vm = wrapper.vm as any
    vm.form.name = 'selport'
    vm.form.label = 'Sel Port'
    vm.form.type = 'select'
    vm.form.default = 'opt-a'
    vm.savePort()
    expect(lastPort(wrapper).default_value).toBe('opt-a')
  })
})
