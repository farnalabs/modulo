import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import BackLink from '../../components/BackLink.vue'

vi.mock('vue-router', () => ({
  useRouter: vi.fn(() => ({
    push: vi.fn(),
    replace: vi.fn(),
    resolve: vi.fn(),
    go: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
  })),
}))

function backLinkCssSource(): string {
  // vitest runs with cwd = the frontend/ vite root; resolve against it so the
  // assertion works regardless of import.meta.url's scheme under transform.
  const source = readFileSync(join(process.cwd(), 'src/components/BackLink.vue'), 'utf-8')
  const match = source.match(/<style[^>]*>([\s\S]*?)<\/style>/)
  if (!match) throw new Error('BackLink.vue has no <style> block')
  return match[1]
}

function createWrapper() {
  return mount(BackLink as any, {
    props: { to: '/foo', label: 'Back to foo' },
    global: {
      stubs: {
        'router-link': {
          template: '<a :href="to"><slot /></a>',
          props: ['to'],
        },
      },
    },
  })
}

describe('BackLink', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the back link with arrow and label', () => {
    const wrapper = createWrapper()
    const link = wrapper.find('.back-link')
    expect(link.exists()).toBe(true)
    expect(link.text()).toContain('Back to foo')
    expect(wrapper.find('.back-arrow').exists()).toBe(true)
  })

  it('enforces a minimum 24px-tall target via min-height + vertical padding', () => {
    // jsdom does not apply SFC scoped styles (vitest css:false stubs them),
    // so assert the class-level CSS that enforces the WCAG 2.5.8 target size.
    const css = backLinkCssSource()
    expect(css).toMatch(/min-height:\s*(1\.5rem|24px)/)
    // 0.375rem top + 0.375rem bottom padding (6px+6px) added to the content
    // line box yields a rendered height >= 24px even on a 0.875rem line.
    expect(css).toMatch(/padding:\s*0\.375rem/)
  })

  it('enforces a minimum 24px-wide target via min-width', () => {
    const css = backLinkCssSource()
    expect(css).toMatch(/min-width:\s*(1\.5rem|24px)/)
  })

  it('keeps the hover state and arrow styling', () => {
    const css = backLinkCssSource()
    expect(css).toMatch(/\.back-link:hover/)
    expect(css).toMatch(/\.back-arrow/)
  })
})
