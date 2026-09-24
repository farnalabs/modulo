import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Mock } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import AssistantContextSources from '../components/assistant/AssistantContextSources.vue'
import { api } from '@/lib/api/client'

vi.mock('@/lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    PUT: vi.fn(),
    DELETE: vi.fn(),
  },
}))

vi.mock('@/lib/api/formatError', () => ({
  formatApiError: (err: unknown) => {
    if (typeof err === 'object' && err !== null && 'detail' in err) return (err as { detail: string }).detail
    if (typeof err === 'string') return err
    if (err instanceof Error) return err.message
    return 'Unknown error'
  },
}))

const apiGet = api.GET as unknown as Mock
const apiPut = api.PUT as unknown as Mock
const apiDelete = api.DELETE as unknown as Mock

function makeSource(overrides: Partial<{ key: string; name: string; description: string; source_mode: 'always_on' | 'tool' | 'off'; is_overridden: boolean }> = {}) {
  return {
    key: overrides.key ?? 'web',
    name: overrides.name ?? 'Web Search',
    description: overrides.description ?? 'Search the web',
    source_mode: overrides.source_mode ?? 'always_on',
    is_overridden: overrides.is_overridden ?? false,
  }
}

function mountSources() {
  return mount(AssistantContextSources, {
    global: {
      stubs: {
        Button: { template: '<button :disabled="disabled" @click="$emit(\'click\')"><slot /></button>', props: ['disabled'] },
      },
    },
  })
}

describe('AssistantContextSources', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('fetches sources on mount and renders them', async () => {
    const sources = [
      makeSource({ key: 'web', name: 'Web Search' }),
      makeSource({ key: 'docs', name: 'Documentation', source_mode: 'tool', is_overridden: true }),
    ]
    apiGet.mockResolvedValue({ data: sources, error: undefined })

    const wrapper = mountSources()
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith('/api/v1/me/assistant/context-sources')
    expect(wrapper.findAll('.assistant-cs-row')).toHaveLength(2)
    expect(wrapper.text()).toContain('Web Search')
    expect(wrapper.text()).toContain('Documentation')
  })

  it('shows loading state while fetching', async () => {
    apiGet.mockReturnValue(new Promise(() => {})) // never resolves

    const wrapper = mountSources()
    await flushPromises()

    expect(wrapper.text()).toContain('Loading')
    expect(wrapper.findAll('.assistant-cs-row')).toHaveLength(0)
  })

  it('shows error state when fetch fails with API error', async () => {
    apiGet.mockResolvedValue({ data: undefined, error: { detail: 'Server error' } })

    const wrapper = mountSources()
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to load sources: Server error')
    expect(wrapper.findAll('.assistant-cs-row')).toHaveLength(0)
  })

  it('shows error state when fetch throws', async () => {
    apiGet.mockRejectedValue(new Error('Network down'))

    const wrapper = mountSources()
    await flushPromises()

    // The catch block returns just e.message, not "Failed to load sources: " prefix
    expect(wrapper.text()).toContain('Network down')
  })

  it('shows "—" for sources with empty description', async () => {
    apiGet.mockResolvedValue({ data: [makeSource({ description: '' })], error: undefined })

    const wrapper = mountSources()
    await flushPromises()

    expect(wrapper.text()).toContain('—')
  })

  it('shows "org default" when source is not overridden', async () => {
    apiGet.mockResolvedValue({ data: [makeSource({ is_overridden: false })], error: undefined })

    const wrapper = mountSources()
    await flushPromises()

    // The i18n text is lowercase in the locale file
    expect(wrapper.text()).toContain('org default')
  })

  it('shows "overridden" when source is overridden', async () => {
    apiGet.mockResolvedValue({ data: [makeSource({ is_overridden: true })], error: undefined })

    const wrapper = mountSources()
    await flushPromises()

    expect(wrapper.text()).toContain('overridden')
  })

  it('displays the current source_mode in the select', async () => {
    apiGet.mockResolvedValue({ data: [makeSource({ source_mode: 'tool' })], error: undefined })

    const wrapper = mountSources()
    await flushPromises()

    const select = wrapper.find('.assistant-cs-select')
    expect((select.element as HTMLSelectElement).value).toBe('tool')
  })

  it('updates source mode optimistically and on success', async () => {
    apiGet.mockResolvedValue({ data: [makeSource({ key: 'web', source_mode: 'always_on' })], error: undefined })
    const updatedSources = [makeSource({ key: 'web', source_mode: 'off' })]
    apiPut.mockResolvedValue({ data: updatedSources, error: undefined })

    const wrapper = mountSources()
    await flushPromises()

    const select = wrapper.find('.assistant-cs-select')
    await select.setValue('off')
    await flushPromises()

    expect(apiPut).toHaveBeenCalledWith('/api/v1/me/assistant/context-sources/{source_key}', {
      params: { path: { source_key: 'web' } },
      body: { source_mode: 'off' },
    })
    // After PUT success, the list is replaced with the API response
    expect(wrapper.findAll('.assistant-cs-row')).toHaveLength(1)
  })

  it('reverts mode on PUT error', async () => {
    apiGet.mockResolvedValue({ data: [makeSource({ key: 'web', source_mode: 'always_on' })], error: undefined })
    apiPut.mockResolvedValue({ data: undefined, error: { detail: 'Permission denied' } })

    const wrapper = mountSources()
    await flushPromises()

    const select = wrapper.find('.assistant-cs-select')
    await select.setValue('off')
    await flushPromises()

    // Mode reverted to original
    expect((select.element as HTMLSelectElement).value).toBe('always_on')
    expect(wrapper.text()).toContain('Failed to update source')
  })

  it('reverts mode on PUT throw', async () => {
    apiGet.mockResolvedValue({ data: [makeSource({ key: 'web', source_mode: 'tool' })], error: undefined })
    apiPut.mockRejectedValue(new Error('Connection lost'))

    const wrapper = mountSources()
    await flushPromises()

    const select = wrapper.find('.assistant-cs-select')
    await select.setValue('off')
    await flushPromises()

    expect((select.element as HTMLSelectElement).value).toBe('tool')
    expect(wrapper.text()).toContain('Connection lost')
  })

  it('disables select while saving', async () => {
    apiGet.mockResolvedValue({ data: [makeSource({ key: 'web' })], error: undefined })
    apiPut.mockReturnValue(new Promise(() => {})) // never resolves

    const wrapper = mountSources()
    await flushPromises()

    const select = wrapper.find('.assistant-cs-select')
    await select.setValue('off')
    await flushPromises()

    expect((select.element as HTMLSelectElement).disabled).toBe(true)
  })

  it('replaces the source list when PUT returns data', async () => {
    apiGet.mockResolvedValue({ data: [makeSource({ key: 'web', source_mode: 'always_on' })], error: undefined })
    const updatedSources = [
      makeSource({ key: 'web', source_mode: 'tool' }),
      makeSource({ key: 'docs', name: 'Docs', source_mode: 'always_on' }),
    ]
    apiPut.mockResolvedValue({ data: updatedSources, error: undefined })

    const wrapper = mountSources()
    await flushPromises()

    const select = wrapper.find('.assistant-cs-select')
    await select.setValue('tool')
    await flushPromises()

    // PUT returned 2 sources, so the list now has 2 rows
    expect(wrapper.findAll('.assistant-cs-row')).toHaveLength(2)
  })

  it('calls DELETE then fetchSources on reset', async () => {
    apiGet.mockResolvedValue({ data: [makeSource({ source_mode: 'off' })], error: undefined })
    apiDelete.mockResolvedValue({ data: undefined, error: undefined })

    const wrapper = mountSources()
    await flushPromises()

    // Initial source is "off"
    expect((wrapper.find('.assistant-cs-select').element as HTMLSelectElement).value).toBe('off')

    await wrapper.find('button').trigger('click')
    await flushPromises()

    expect(apiDelete).toHaveBeenCalledWith('/api/v1/me/assistant/context-sources')
    // After reset, fetchSources is called again (at least 2 total: mount + reset)
    expect(apiGet.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('shows error when reset fails with API error', async () => {
    apiGet.mockResolvedValue({ data: [makeSource()], error: undefined })
    apiDelete.mockResolvedValue({ data: undefined, error: { detail: 'Cannot reset' } })

    const wrapper = mountSources()
    await flushPromises()

    await wrapper.find('button').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to reset sources')
  })

  it('shows error when reset throws', async () => {
    apiGet.mockResolvedValue({ data: [makeSource()], error: undefined })
    apiDelete.mockRejectedValue(new Error('Reset failed'))

    const wrapper = mountSources()
    await flushPromises()

    await wrapper.find('button').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Reset failed')
  })

  it('disables reset button while resetting', async () => {
    apiGet.mockResolvedValue({ data: [makeSource()], error: undefined })
    apiDelete.mockReturnValue(new Promise(() => {})) // never resolves

    const wrapper = mountSources()
    await flushPromises()

    const resetBtn = wrapper.find('button')
    await resetBtn.trigger('click')
    await flushPromises()

    expect((resetBtn.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows key tooltip on the info icon span', async () => {
    apiGet.mockResolvedValue({ data: [makeSource({ key: 'my-source' })], error: undefined })

    const wrapper = mountSources()
    await flushPromises()

    // The title attribute is on the <span> wrapping the SVG, not the SVG itself
    const infoSpan = wrapper.find('.assistant-cs-row span[title]')
    expect(infoSpan.exists()).toBe(true)
    expect(infoSpan.attributes('title')).toBe('Key: my-source')
  })
})
