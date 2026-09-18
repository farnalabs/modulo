/**
 * Coverage-focused tests for StageNode.vue (FAR-835).
 *
 * Covers: all stage_type branches in computed properties (stageTypeLabel,
 * borderClass, bgClass, labelClass), graduated badge visibility, description
 * rendering, owner rendering, "Untitled Stage" fallback, and selected ring
 * class.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

import StageNode from '../components/lifecycle-map/editor/StageNode.vue'

/* ── Stubs for @vue-flow/core and @lucide/vue ────────────────────────── */

const HandleStub = {
  template: '<div data-testid="handle" :data-type="type" :data-position="position" />',
  props: ['type', 'position'],
}

function mountNode(props: Partial<InstanceType<typeof StageNode>['$props']> = {}) {
  return mount(StageNode, {
    props: {
      data: {
        name: 'Build Stage',
        description: '',
        stage_type: 'modulo',
        owner: null,
        graduated: false,
      },
      selected: false,
      id: 'node-1',
      ...props,
    },
    global: {
      stubs: {
        Handle: HandleStub,
      },
    },
  })
}

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.unstubAllGlobals() })

/* ── stageTypeLabel computed ──────────────────────────────────────────── */

describe('StageNode stageTypeLabel', () => {
  it('returns "Modulo" for modulo type', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'modulo', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { stageTypeLabel: string }
    expect(vm.stageTypeLabel).toBe('Modulo')
  })

  it('returns "External" for external type', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'external', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { stageTypeLabel: string }
    expect(vm.stageTypeLabel).toBe('External')
  })

  it('returns "Manual" for manual type', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'manual', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { stageTypeLabel: string }
    expect(vm.stageTypeLabel).toBe('Manual')
  })

  it('returns "Placeholder" for placeholder type', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'placeholder', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { stageTypeLabel: string }
    expect(vm.stageTypeLabel).toBe('Placeholder')
  })

  it('returns "Stage" for unknown type (default branch)', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'unknown' as never, owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { stageTypeLabel: string }
    expect(vm.stageTypeLabel).toBe('Stage')
  })
})

/* ── borderClass computed ─────────────────────────────────────────────── */

describe('StageNode borderClass', () => {
  it('returns primary/60 for modulo', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'modulo', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { borderClass: string }
    expect(vm.borderClass).toContain('border-primary/60')
  })

  it('returns dashed sky border for external', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'external', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { borderClass: string }
    expect(vm.borderClass).toContain('border-dashed')
    expect(vm.borderClass).toContain('border-sky-500/60')
  })

  it('returns dotted amber border for manual', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'manual', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { borderClass: string }
    expect(vm.borderClass).toContain('border-dotted')
    expect(vm.borderClass).toContain('border-amber-500/60')
  })

  it('returns dashed muted border for placeholder', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'placeholder', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { borderClass: string }
    expect(vm.borderClass).toContain('border-dashed')
    expect(vm.borderClass).toContain('border-muted-foreground/30')
  })

  it('returns default border-border for unknown type', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'unknown' as never, owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { borderClass: string }
    expect(vm.borderClass).toBe('border-border')
  })
})

/* ── bgClass computed ─────────────────────────────────────────────────── */

describe('StageNode bgClass', () => {
  it('returns primary/5 for modulo', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'modulo', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { bgClass: string }
    expect(vm.bgClass).toBe('bg-primary/5')
  })

  it('returns sky/5 for external', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'external', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { bgClass: string }
    expect(vm.bgClass).toBe('bg-sky-500/5')
  })

  it('returns amber/5 for manual', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'manual', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { bgClass: string }
    expect(vm.bgClass).toBe('bg-amber-500/5')
  })

  it('returns muted/20 for placeholder', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'placeholder', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { bgClass: string }
    expect(vm.bgClass).toBe('bg-muted/20')
  })

  it('returns bg-card for unknown type', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'unknown' as never, owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { bgClass: string }
    expect(vm.bgClass).toBe('bg-card')
  })
})

/* ── labelClass computed ──────────────────────────────────────────────── */

describe('StageNode labelClass', () => {
  it('returns text-primary for modulo', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'modulo', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { labelClass: string }
    expect(vm.labelClass).toBe('text-primary')
  })

  it('returns text-sky-500 for external', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'external', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { labelClass: string }
    expect(vm.labelClass).toBe('text-sky-500')
  })

  it('returns text-amber-500 for manual', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'manual', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { labelClass: string }
    expect(vm.labelClass).toBe('text-amber-500')
  })

  it('returns text-muted-foreground for placeholder', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'placeholder', owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { labelClass: string }
    expect(vm.labelClass).toBe('text-muted-foreground')
  })

  it('returns text-foreground for unknown type', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'unknown' as never, owner: null, graduated: false } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { labelClass: string }
    expect(vm.labelClass).toBe('text-foreground')
  })
})

/* ── Template content ─────────────────────────────────────────────────── */

describe('StageNode template', () => {
  it('renders the stage name', async () => {
    const wrapper = mountNode({ data: { name: 'Deploy', description: '', stage_type: 'modulo', owner: null, graduated: false } })
    await flushPromises()
    expect(wrapper.text()).toContain('Deploy')
  })

  it('renders "Untitled Stage" when name is empty', async () => {
    const wrapper = mountNode({ data: { name: '', description: '', stage_type: 'modulo', owner: null, graduated: false } })
    await flushPromises()
    expect(wrapper.text()).toContain('Untitled Stage')
  })

  it('renders description when present', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: 'Builds the project', stage_type: 'modulo', owner: null, graduated: false } })
    await flushPromises()
    expect(wrapper.text()).toContain('Builds the project')
  })

  it('does not render description when empty', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'modulo', owner: null, graduated: false } })
    await flushPromises()
    expect(wrapper.find('.line-clamp-2').exists()).toBe(false)
  })

  it('renders owner when present', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'modulo', owner: 'team-alpha', graduated: false } })
    await flushPromises()
    expect(wrapper.text()).toContain('team-alpha')
  })

  it('does not render owner when null', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'modulo', owner: null, graduated: false } })
    await flushPromises()
    expect(wrapper.find('.bg-muted').exists()).toBe(false)
  })

  it('shows graduated badge when graduated is true', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'modulo', owner: null, graduated: true } })
    await flushPromises()
    expect(wrapper.text()).toContain('Graduated')
  })

  it('does not show graduated badge when graduated is false', async () => {
    const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: 'modulo', owner: null, graduated: false } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('Graduated')
  })
})

/* ── Selected ring class ──────────────────────────────────────────────── */

describe('StageNode selected', () => {
  it('applies ring-2 ring-primary when selected', async () => {
    const wrapper = mountNode({ selected: true })
    await flushPromises()
    expect(wrapper.find('.ring-2').exists()).toBe(true)
    expect(wrapper.find('.ring-primary').exists()).toBe(true)
  })

  it('does not apply ring classes when not selected', async () => {
    const wrapper = mountNode({ selected: false })
    await flushPromises()
    expect(wrapper.find('.ring-2').exists()).toBe(false)
  })
})

/* ── Handles ──────────────────────────────────────────────────────────── */

describe('StageNode handles', () => {
  it('renders two handles (target and source)', async () => {
    const wrapper = mountNode()
    await flushPromises()
    const handles = wrapper.findAll('[data-testid="handle"]')
    expect(handles).toHaveLength(2)
  })

  it('renders a target handle at Top position', async () => {
    const wrapper = mountNode()
    await flushPromises()
    const handles = wrapper.findAll('[data-testid="handle"]')
    const target = handles.find(h => h.attributes('data-type') === 'target')
    expect(target).toBeDefined()
    expect(target!.attributes('data-position')).toBe('top')
  })

  it('renders a source handle at Bottom position', async () => {
    const wrapper = mountNode()
    await flushPromises()
    const handles = wrapper.findAll('[data-testid="handle"]')
    const source = handles.find(h => h.attributes('data-type') === 'source')
    expect(source).toBeDefined()
    expect(source!.attributes('data-position')).toBe('bottom')
  })
})

/* ── Prop passthrough ─────────────────────────────────────────────────── */

describe('StageNode props', () => {
  it('passes id prop', async () => {
    const wrapper = mountNode({ id: 'custom-id' })
    await flushPromises()
    const vm = wrapper.vm as unknown as { id: string }
    expect(vm.id).toBe('custom-id')
  })
})

/* ── Stage type label in template ─────────────────────────────────────── */

describe('StageNode stage type label in template', () => {
  it('renders the stage type label text for each type', async () => {
    const types = ['modulo', 'external', 'manual', 'placeholder'] as const
    for (const stageType of types) {
      const wrapper = mountNode({ data: { name: 'Test', description: '', stage_type: stageType, owner: null, graduated: false } })
      await flushPromises()
      const expectedLabel = { modulo: 'Modulo', external: 'External', manual: 'Manual', placeholder: 'Placeholder' }[stageType]
      expect(wrapper.text()).toContain(expectedLabel)
    }
  })
})
