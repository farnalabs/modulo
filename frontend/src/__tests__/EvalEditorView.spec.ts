import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    del: vi.fn().mockResolvedValue(undefined),
  })),
}))

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] } }),
    POST: vi.fn().mockResolvedValue({ data: {} }),
    PUT: vi.fn().mockResolvedValue({ data: {} }),
    DELETE: vi.fn().mockResolvedValue({ data: {} }),
  },
}))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

import EvalEditorView from '../views/EvalEditorView.vue'

describe('EvalEditorView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(EvalEditorView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('views.EvalEditorView.eval_editor')
  })

  it('renders a per-type config placeholder on the textarea', async () => {
    const wrapper = mount(EvalEditorView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    const textarea = await vi.waitFor(() => {
      const el = wrapper.find('[data-testid="eval-editor-config"]')
      expect(el.exists()).toBe(true)
      return el
    })
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.llm_judge')

    ;(wrapper.vm as any).form.eval_type = 'regex'
    await nextTick()
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.regex')

    ;(wrapper.vm as any).form.eval_type = 'json_schema'
    await nextTick()
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.json_schema')

    ;(wrapper.vm as any).form.eval_type = 'custom_function'
    await nextTick()
    expect(textarea.attributes('placeholder')).toBe('views.EvalEditorView.configPlaceholder.custom_function')
  })
})
