import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import KnownFixesPanel from '../components/shared/KnownFixesPanel.vue'

describe('KnownFixesPanel', () => {
  it('renders a matched known fix with title, body and documentation link', () => {
    const wrapper = mount(KnownFixesPanel, {
      props: {
        fixes: [
          {
            fix_id: 'heredoc_at_end_of_command',
            title: 'Here-document at the end of an agent command',
            body: 'Emit the payload as a base64 block instead of a heredoc.',
            link: 'https://example.com/docs',
          },
        ],
      },
    })

    const panel = wrapper.find('[data-testid="run-detail-known-fixes"]')
    expect(panel.exists()).toBe(true)
    expect(panel.attributes('aria-live')).toBe('polite')
    expect(wrapper.text()).toContain('Known fixes')
    expect(wrapper.text()).toContain('Here-document at the end of an agent command')
    expect(wrapper.text()).toContain('Emit the payload as a base64 block instead of a heredoc.')
    const link = wrapper.find('[data-testid="run-detail-known-fix-heredoc_at_end_of_command"] a')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('https://example.com/docs')
  })

  it('renders nothing when the list is empty (no empty box)', () => {
    const wrapper = mount(KnownFixesPanel, { props: { fixes: [] } })
    expect(wrapper.find('[data-testid="run-detail-known-fixes"]').exists()).toBe(false)
    expect(wrapper.text()).toBe('')
  })

  it('renders nothing when fixes are absent or null', () => {
    const absent = mount(KnownFixesPanel)
    expect(absent.find('[data-testid="run-detail-known-fixes"]').exists()).toBe(false)

    const nulled = mount(KnownFixesPanel, { props: { fixes: null } })
    expect(nulled.find('[data-testid="run-detail-known-fixes"]').exists()).toBe(false)
  })

  it('drops malformed entries instead of breaking the page', () => {
    const wrapper = mount(KnownFixesPanel, {
      props: {
        fixes: [
          null as never,
          { fix_id: 'broken', title: '', body: '' },
          { fix_id: 'ok', title: 'Valid fix title', body: 'Valid fix body.' },
        ],
      },
    })

    const panel = wrapper.find('[data-testid="run-detail-known-fixes"]')
    expect(panel.exists()).toBe(true)
    expect(wrapper.text()).toContain('Valid fix title')
    expect(wrapper.text()).not.toContain('broken')
    expect(wrapper.findAll('article')).toHaveLength(1)
  })

  it('omits the documentation link when no link is provided', () => {
    const wrapper = mount(KnownFixesPanel, {
      props: {
        fixes: [{ fix_id: 'no_link', title: 'A fix', body: 'The body.' }],
      },
    })

    expect(wrapper.find('[data-testid="run-detail-known-fixes"] a').exists()).toBe(false)
  })

  it('renders entries without a fix_id, keyed by list index', () => {
    const wrapper = mount(KnownFixesPanel, {
      props: {
        fixes: [
          { title: 'Fix without an id', body: 'Body without an id.' },
          { fix_id: 'with_id', title: 'Fix with an id', body: 'Body with an id.' },
        ],
      },
    })

    const articles = wrapper.findAll('article')
    expect(articles).toHaveLength(2)
    expect(wrapper.find('[data-testid="run-detail-known-fix-0"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="run-detail-known-fix-with_id"]').exists()).toBe(true)
  })
})
