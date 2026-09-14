import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import SandboxCommandsEditor from '../components/pipeline/SandboxCommandsEditor.vue'

// Harness wires the exact v-model contract the pipeline editor uses, so
// emitted updates feed back into props like they do in the real parent.
function mountHarness(initial: { commands?: string[]; joiner?: string } = {}) {
  const commands = ref<string[] | null>(initial.commands ?? null)
  const joiner = ref<string | null>(initial.joiner ?? null)
  const Harness = defineComponent({
    name: 'SandboxCommandsEditorHarness',
    setup() {
      return () =>
        h(SandboxCommandsEditor, {
          commands: commands.value,
          'onUpdate:commands': (v: string[]) => {
            commands.value = v
          },
          joiner: joiner.value,
          'onUpdate:joiner': (v: string) => {
            joiner.value = v
          },
        })
    },
  })
  return { wrapper: mount(Harness), commands, joiner }
}

describe('SandboxCommandsEditor', () => {
  it('shows empty state when no commands are provided', () => {
    const { wrapper } = mountHarness()
    expect(wrapper.find('[data-testid="pipeline-editor-node-command-empty"]').exists()).toBe(true)
  })

  it('renders pre-existing command-list rows (read-back legibility)', () => {
    const { wrapper } = mountHarness({ commands: ['cmd-a', 'cmd-b'], joiner: ' ; ' })
    expect((wrapper.find('[data-testid="pipeline-editor-node-command-row-0"]').element as HTMLInputElement).value).toBe('cmd-a')
    expect((wrapper.find('[data-testid="pipeline-editor-node-command-row-1"]').element as HTMLInputElement).value).toBe('cmd-b')
    const preview = wrapper.find('[data-testid="pipeline-editor-node-command-preview"]')
    expect(preview.exists()).toBe(true)
    expect(preview.text()).toContain('cmd-a ; cmd-b')
  })

  it('renders a single-item list as a one-row editor', () => {
    const { wrapper } = mountHarness({ commands: ['opencode run --auto'] })
    expect((wrapper.find('[data-testid="pipeline-editor-node-command-row-0"]').element as HTMLInputElement).value).toBe('opencode run --auto')
    expect(wrapper.find('[data-testid="pipeline-editor-node-command-empty"]').exists()).toBe(false)
    // joiner UI is always shown
    expect(wrapper.find('[data-testid="pipeline-editor-node-command-joiner"]').exists()).toBe(true)
  })

  it('authoring a two-command list emits the array and previews with the default joiner', async () => {
    const { wrapper, commands } = mountHarness()
    await wrapper.find('[data-testid="pipeline-editor-node-command-add"]').trigger('click')
    await wrapper.find('[data-testid="pipeline-editor-node-command-row-0"]').setValue('opencode run')
    await wrapper.find('[data-testid="pipeline-editor-node-command-add"]').trigger('click')
    await wrapper.find('[data-testid="pipeline-editor-node-command-row-1"]').setValue('--model oxf')

    expect(commands.value).toEqual(['opencode run', '--model oxf'])
    const preview = wrapper.find('[data-testid="pipeline-editor-node-command-preview"]')
    expect(preview.text()).toContain('opencode run && --model oxf')
  })

  it('a custom join operator is emitted and used by the preview', async () => {
    const { wrapper, joiner } = mountHarness({ commands: ['cmd-a', 'cmd-b'] })
    await wrapper.find('[data-testid="pipeline-editor-node-command-joiner"]').setValue(' ; ')
    expect(joiner.value).toBe(' ; ')
    expect(wrapper.find('[data-testid="pipeline-editor-node-command-preview"]').text()).toContain('cmd-a ; cmd-b')
  })

  it('removes a row and emits the remaining list', async () => {
    const { wrapper, commands } = mountHarness({ commands: ['cmd-a', 'cmd-b'] })
    await wrapper.find('[data-testid="pipeline-editor-node-command-remove-0"]').trigger('click')
    expect(commands.value).toEqual(['cmd-b'])
  })

  it('reorders rows with the up/down controls (disabled at the edges)', async () => {
    const { wrapper, commands } = mountHarness({ commands: ['cmd-a', 'cmd-b', 'cmd-c'] })

    const up0 = wrapper.find('[data-testid="pipeline-editor-node-command-up-0"]')
    const down2 = wrapper.find('[data-testid="pipeline-editor-node-command-down-2"]')
    expect((up0.element as HTMLButtonElement).disabled).toBe(true)
    expect((down2.element as HTMLButtonElement).disabled).toBe(true)

    await wrapper.find('[data-testid="pipeline-editor-node-command-up-1"]').trigger('click')
    expect(commands.value).toEqual(['cmd-b', 'cmd-a', 'cmd-c'])

    await wrapper.find('[data-testid="pipeline-editor-node-command-down-0"]').trigger('click')
    expect(commands.value).toEqual(['cmd-a', 'cmd-b', 'cmd-c'])
  })

  it('keeps in-progress empty rows locally (the save path filters them, not the editor)', async () => {
    const { wrapper, commands } = mountHarness()
    await wrapper.find('[data-testid="pipeline-editor-node-command-add"]').trigger('click')
    await wrapper.find('[data-testid="pipeline-editor-node-command-row-0"]').setValue('cmd-a')
    // second row added but left empty — it must NOT be dropped while editing
    await wrapper.find('[data-testid="pipeline-editor-node-command-add"]').trigger('click')
    expect(commands.value).toEqual(['cmd-a', ''])
  })

  it('labels rows and icon-only buttons for accessibility', () => {
    const { wrapper } = mountHarness({ commands: ['cmd-a', 'cmd-b'] })
    const row0 = wrapper.find('[data-testid="pipeline-editor-node-command-row-0"]')
    expect(row0.attributes('aria-label')).toContain('1')
    for (const idx of [0, 1]) {
      for (const action of ['up', 'down', 'remove']) {
        const btn = wrapper.find(`[data-testid="pipeline-editor-node-command-${action}-${idx}"]`)
        expect(btn.exists()).toBe(true)
        expect(btn.attributes('aria-label')).toBeTruthy()
      }
    }
  })
})
