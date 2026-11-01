import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import FormDialog from '../../components/shared/FormDialog.vue'

function mountDialog(props: Record<string, unknown> = {}) {
  return mount(FormDialog, {
    props: { open: true, title: 'Confirm action', ...props },
    global: {
      stubs: {
        Dialog: {
          props: ['visible'],
          template: '<div v-if="visible"><slot name="header" /><slot /><slot name="footer" /></div>',
        },
        Button: {
          props: ['disabled', 'loading'],
          template: '<button :disabled="disabled" :data-loading="loading"><slot /></button>',
        },
      },
    },
  })
}

describe('FormDialog', () => {
  it('renders normal confirm text', () => {
    expect(mountDialog({ confirmText: 'Delete' }).findAll('button')[1].text()).toBe('Delete')
  })

  it('renders loading text while confirming', () => {
    const button = mountDialog({ confirmText: 'Delete', confirmLoadingText: 'Deleting...', loading: true }).findAll('button')[1]
    expect(button.text()).toBe('Deleting...')
    expect((button.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('falls back to confirm text when loading text is omitted', () => {
    expect(mountDialog({ confirmText: 'Delete', loading: true }).findAll('button')[1].text()).toBe('Delete')
  })

  it('disables confirmation when explicitly disabled', () => {
    expect((mountDialog({ confirmText: 'Delete', confirmDisabled: true }).findAll('button')[1].element as HTMLButtonElement).disabled).toBe(true)
  })
})
