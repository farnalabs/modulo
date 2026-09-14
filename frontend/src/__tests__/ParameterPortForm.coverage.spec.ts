import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ParameterPortForm from '../components/pipeline/composite/ParameterPortForm.vue'
import type { ParameterPort } from '../types/pipeline'

function makePort(type: ParameterPort['type'], default_value?: unknown): ParameterPort {
  return {
    id: 'p1',
    name: 'param',
    label: 'Param',
    type,
    required: false,
    default_value,
    multiline: false,
    target_injection: { mode: 'prompt_replace', node_id: '', injection_point: 'prompt_template' },
  }
}

describe('ParameterPortForm resolveDefaultFallback (SonarCloud coverage)', () => {
  it('falls back to false for boolean ports with no value', () => {
    const wrapper = mount(ParameterPortForm, {
      props: { port: makePort('boolean'), modelValue: undefined },
    })
    expect((wrapper.vm as any).localValue).toBe(false)
  })

  it('falls back to 0 for number ports with no value', () => {
    const wrapper = mount(ParameterPortForm, {
      props: { port: makePort('number'), modelValue: undefined },
    })
    expect((wrapper.vm as any).localValue).toBe(0)
  })

  it('falls back to empty string for string ports with no value', () => {
    const wrapper = mount(ParameterPortForm, {
      props: { port: makePort('string'), modelValue: undefined },
    })
    expect((wrapper.vm as any).localValue).toBe('')
  })

  it('prefers an explicit default_value over the fallback', () => {
    const wrapper = mount(ParameterPortForm, {
      props: { port: makePort('string', 'preset'), modelValue: undefined },
    })
    expect((wrapper.vm as any).localValue).toBe('preset')
  })
})
