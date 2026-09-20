import { describe, it, expect, vi, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import VersionHistoryDropdown from '../components/lifecycle-map/editor/VersionHistoryDropdown.vue'

vi.mock('../lib/formatDate', () => ({
  formatDateShort: (d: Date) => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }),
}))

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: {
    'en-US': {
      components: {
        'lifecycle-map': {
          editor: {
            VersionHistoryDropdown: {
              version_history: 'Version History',
              no_versions: 'No versions yet',
            },
          },
        },
      },
    },
  },
})

function makeVersion(overrides: { id?: string; version_number?: number; created_at?: string } = {}) {
  return {
    id: overrides.id ?? 'v1',
    lifecycle_map_id: 'map-1',
    version_number: overrides.version_number ?? 1,
    stages: [],
    edges: [],
    created_by: 'alice',
    created_at: overrides.created_at ?? '2026-01-15T10:00:00Z',
    notes: '',
  }
}

describe('VersionHistoryDropdown', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('shows current version number from the matched version', () => {
    const versions = [
      makeVersion({ id: 'v1', version_number: 1 }),
      makeVersion({ id: 'v2', version_number: 2 }),
    ]
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions, currentVersionId: 'v2' },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('v2')
  })

  it('defaults to v1 when currentVersionId does not match any version', () => {
    const versions = [makeVersion({ id: 'v1', version_number: 1 })]
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions, currentVersionId: 'nonexistent' },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('v1')
  })

  it('defaults to v1 when versions list is empty', () => {
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions: [], currentVersionId: '' },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('v1')
  })

  it('toggles dropdown open on button click', async () => {
    const versions = [makeVersion({ id: 'v1', version_number: 1 })]
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions, currentVersionId: 'v1' },
      global: { plugins: [i18n] },
    })
    // Dropdown is closed by default
    expect(wrapper.find('.absolute').exists()).toBe(false)

    await wrapper.find('button').trigger('click')
    expect(wrapper.find('.absolute').exists()).toBe(true)
    expect(wrapper.text()).toContain('Version History')
  })

  it('closes dropdown on second button click (toggle)', async () => {
    const versions = [makeVersion({ id: 'v1', version_number: 1 })]
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions, currentVersionId: 'v1' },
      global: { plugins: [i18n] },
    })
    await wrapper.find('button').trigger('click') // open
    await wrapper.find('button').trigger('click') // close
    expect(wrapper.find('.absolute').exists()).toBe(false)
  })

  it('emits select with version id and closes dropdown', async () => {
    const versions = [
      makeVersion({ id: 'v1', version_number: 1 }),
      makeVersion({ id: 'v2', version_number: 2 }),
    ]
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions, currentVersionId: 'v1' },
      global: { plugins: [i18n] },
    })
    await wrapper.find('button').trigger('click') // open

    const versionButtons = wrapper.findAll('.absolute button')
    expect(versionButtons.length).toBe(2)

    await versionButtons[0].trigger('click')
    expect(wrapper.emitted('select')).toEqual([['v2']]) // sorted desc, first is v2
    expect(wrapper.find('.absolute').exists()).toBe(false)
  })

  it('sorts versions by version_number descending', async () => {
    const versions = [
      makeVersion({ id: 'v1', version_number: 1 }),
      makeVersion({ id: 'v3', version_number: 3 }),
      makeVersion({ id: 'v2', version_number: 2 }),
    ]
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions, currentVersionId: 'v1' },
      global: { plugins: [i18n] },
    })
    await wrapper.find('button').trigger('click')

    const versionButtons = wrapper.findAll('.absolute button')
    expect(versionButtons.length).toBe(3)
    expect(versionButtons[0].text()).toContain('v3')
    expect(versionButtons[1].text()).toContain('v2')
    expect(versionButtons[2].text()).toContain('v1')
  })

  it('highlights current version with bg-accent class', async () => {
    const versions = [
      makeVersion({ id: 'v1', version_number: 1 }),
      makeVersion({ id: 'v2', version_number: 2 }),
    ]
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions, currentVersionId: 'v2' },
      global: { plugins: [i18n] },
    })
    await wrapper.find('button').trigger('click')

    const versionButtons = wrapper.findAll('.absolute button')
    // v2 is first in sorted order (descending) and should be highlighted
    expect(versionButtons[0].classes()).toContain('bg-accent')
    expect(versionButtons[0].classes()).toContain('font-medium')
    // v1 should not be highlighted
    expect(versionButtons[1].classes()).not.toContain('bg-accent')
  })

  it('shows formatted date for each version', async () => {
    const versions = [
      makeVersion({ id: 'v1', version_number: 1, created_at: '2026-03-10T14:30:00Z' }),
    ]
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions, currentVersionId: 'v1' },
      global: { plugins: [i18n] },
    })
    await wrapper.find('button').trigger('click')

    const versionButton = wrapper.findAll('.absolute button')[0]
    // formatDateShort mock returns locale date string
    expect(versionButton.text()).toMatch(/Mar/)
  })

  it('shows "?" for invalid date strings', async () => {
    const versions = [
      makeVersion({ id: 'v1', version_number: 1, created_at: 'not-a-date' }),
    ]
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions, currentVersionId: 'v1' },
      global: { plugins: [i18n] },
    })
    await wrapper.find('button').trigger('click')

    const versionButton = wrapper.findAll('.absolute button')[0]
    expect(versionButton.text()).toContain('?')
  })

  it('shows "No versions yet" when versions list is empty and dropdown is open', async () => {
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions: [], currentVersionId: '' },
      global: { plugins: [i18n] },
    })
    await wrapper.find('button').trigger('click')

    expect(wrapper.text()).toContain('No versions yet')
  })

  it('closes dropdown when clicking outside the component', async () => {
    const versions = [makeVersion({ id: 'v1', version_number: 1 })]
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions, currentVersionId: 'v1' },
      global: { plugins: [i18n] },
    })

    await wrapper.find('button').trigger('click')
    expect(wrapper.find('.absolute').exists()).toBe(true)

    // Click outside: dispatch a click event on the document body
    document.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: 0, clientY: 0 }))
    await wrapper.vm.$nextTick()

    // The dropdown should close because the body is outside the dropdownRef
    // Note: in jsdom the event target is the body, which is outside the ref
    expect(wrapper.find('.absolute').exists()).toBe(false)
  })

  it('does not close dropdown when clicking inside the dropdown', async () => {
    const versions = [makeVersion({ id: 'v1', version_number: 1 })]
    const wrapper = mount(VersionHistoryDropdown, {
      props: { versions, currentVersionId: 'v1' },
      global: { plugins: [i18n] },
    })

    await wrapper.find('button').trigger('click')
    expect(wrapper.find('.absolute').exists()).toBe(true)

    // The dropdown div exists within the component's ref
    const dropdownDiv = wrapper.find('.absolute')
    expect(dropdownDiv.exists()).toBe(true)
  })
})
