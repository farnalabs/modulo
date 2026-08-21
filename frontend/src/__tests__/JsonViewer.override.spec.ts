import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import JsonViewer from '../components/shared/JsonViewer.vue'

const fixture = {
  name: 'Alice',
  age: 30,
  active: true,
  note: null,
  tags: ['a', 'b'],
  meta: { level: 2 },
}

// A second, richer fixture so overrides targeting collapsed/nested nodes
// (e.g. indent guide lines, node actions) have elements to match.
// `showIcon`/`renderNodeActions` render the dep's carets + per-node actions,
// which are otherwise disabled.
const nestedFixture = {
  items: Array.from({ length: 25 }, (_, i) => ({ id: i, label: `item-${i}` })),
  deep: { a: { b: { c: { d: 1 } } } },
}

// >500 chars so the long-string truncation path fires for every string.
const longValue = 'A'.repeat(1200)
const longFixture = { agent_stdout: longValue }

function mountSimple() {
  return mount(JsonViewer, { props: { data: fixture } })
}

function mountNested() {
  return mount(JsonViewer, {
    props: {
      data: nestedFixture,
      showToolbar: true,
      collapsedNodeLength: 5,
      showIcon: true,
      renderNodeActions: true,
    },
  })
}

function mountLong() {
  return mount(JsonViewer, { props: { data: longFixture } })
}

/** A long raw (non-JSON) string hits the plain-string render path. */
function mountPlainLong() {
  return mount(JsonViewer, { props: { data: longValue } })
}

/** Stub `navigator.clipboard` so copyValue() can be asserted in jsdom. */
function stubClipboard(): ReturnType<typeof vi.fn> {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
    writable: true,
  })
  return writeText
}

/** The rendered `.vjs-tree` element inside a mounted wrapper (or null). */
function treeElement(wrapper: ReturnType<typeof mountSimple>): Element | null {
  return wrapper.element.querySelector('.json-viewer .vjs-tree')
}

/**
 * jsdom's CSS selector engine (nwsapi) fails to resolve the descendant
 * combinator when the target class name contains the literal token `null`
 * (`.json-viewer .vjs-value-null` matches nothing even though the element
 * is in the tree), and it also cannot resolve a leading `.json-viewer`
 * compound when queried from inside the `.vjs-tree` scope. Walking the DOM
 * with `element.matches(selector)` avoids both parser quirks while still
 * asserting the selector resolves inside the component.
 */
function walkMatches(root: Element, selector: string): boolean {
  return root.matches(selector) || Array.from(root.querySelectorAll('*')).some((el) => el.matches(selector))
}

function classPresentInTree(wrapper: ReturnType<typeof mountSimple>, cls: string): boolean {
  const tree = treeElement(wrapper)
  if (!tree) return false
  return walkMatches(tree, cls)
}

/** Find OUR override stylesheet (identifiable by any `.json-viewer` rule). */
function findOverrideSheet(): CSSStyleSheet | null {
  for (let i = 0; i < document.styleSheets.length; i++) {
    const sheet = document.styleSheets[i]
    let rules: CSSRuleList | null = null
    try {
      rules = sheet.cssRules
    } catch {
      continue
    }
    if (!rules) continue
    for (let j = 0; j < rules.length; j++) {
      const rule = rules[j]
      if (rule instanceof CSSStyleRule && rule.selectorText.includes('.json-viewer')) {
        return sheet
      }
    }
  }
  return null
}

function ruleSelectors(sheet: CSSStyleSheet | null): string[] {
  if (!sheet) return []
  const selectors: string[] = []
  try {
    for (let j = 0; j < sheet.cssRules.length; j++) {
      const rule = sheet.cssRules[j]
      if (rule instanceof CSSStyleRule) selectors.push(rule.selectorText)
    }
  } catch (err) {
    // jsdom can throw on foreign/at-rule boundaries — skip.
    console.warn('Failed to read cssRules', err)
  }
  return selectors
}

/**
 * Reduce a CSS selector to the form querySelector can resolve in jsdom.
 * - jsdom has no hover/focus state and no scrollbar pseudo-elements, so
 *   pseudo-classes/pseudo-elements are stripped.
 * - `.dark` / `.is-highlight` classes only render when the dep's `theme`
 *   prop or node-selection is enabled (we never enable either), so they are
 *   stripped down to the base class — a dep rename of the base `.vjs-*`
 *   class still fails this probe.
 */
function toMatchableSelector(selector: string): string {
  return selector
    .replace(/::?[\w-]+(?:\([^)]*\))?/g, '')
    .replace(/\.(dark|is-highlight)/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

/** Does the probe selector resolve inside either mount's tree scope? */
function selectorMatches(selector: string, wrappers: ReturnType<typeof mountSimple>[]): boolean {
  const probe = toMatchableSelector(selector)
  return wrappers.some((wrapper) => {
    const tree = treeElement(wrapper)
    return tree !== null && walkMatches(tree, probe)
  })
}

function tokenNamesFromCss(css: string): string[] {
  const matches = css.match(/var\((--[\w-]+)\)/g) ?? []
  return [...new Set(matches.map((m) => m.slice(4, -1)))]
}

function tokenDefinedInStyleCss(styleCss: string, token: string): boolean {
  // e.g. `--primary:` appears once per theme block (dark default, .light, [data-theme="agent"])
  return styleCss.includes(`${token}:`)
}

describe('JsonViewer override contract', () => {
  it('mounts with a testid wrapper and toolbar buttons', () => {
    const wrapper = mountSimple()
    expect(wrapper.find('[data-testid="json-viewer"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="json-viewer-copy"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="json-viewer-expand-all"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="json-viewer-collapse-all"]').exists()).toBe(true)
  })

  it('renders every dep class the override CSS depends on', () => {
    const simple = mountSimple()
    const nested = mountNested()
    const classes = [
      '.vjs-tree-node',
      '.vjs-key',
      '.vjs-value-string',
      '.vjs-value-number',
      '.vjs-value-boolean',
      '.vjs-value-null',
      '.vjs-carets',
      '.vjs-tree-brackets',
      '.vjs-indent-unit',
    ]
    for (const cls of classes) {
      const found = classPresentInTree(simple, cls) || classPresentInTree(nested, cls)
      expect(found, `expected to find "${cls}" in the rendered tree`).toBe(true)
    }
  })

  it('every override selector matches an element in at least one mount', async () => {
    const simple = mountSimple()
    const nested = mountNested()
    const longCollapsed = mountLong()
    const longExpanded = mountLong()
    // Expand one long string so the expanded-state selectors have an element.
    await longExpanded.get('[data-testid="json-viewer-string-expand"]').trigger('click')
    await nextTick()
    const plainLong = mountPlainLong()
    const sheet = findOverrideSheet()
    expect(sheet, 'expected our json-viewer override stylesheet to be mounted').not.toBeNull()

    const selectors = ruleSelectors(sheet)
    expect(selectors.length).toBeGreaterThan(0)

    const wrappers = [simple, nested, longCollapsed, longExpanded, plainLong]
    for (const selector of selectors) {
      expect(
        selectorMatches(selector, wrappers),
        `override selector "${selector}" matched no element in any mount`,
      ).toBe(true)
    }
  })

  it('every design token used by json-viewer.css is defined in style.css', () => {
    // Vitest transforms the test module, so `import.meta.url` is not a
    // filesystem path — resolve from the frontend cwd instead.
    const overridePath = resolve(process.cwd(), 'src/components/shared/json-viewer.css')
    const stylePath = resolve(process.cwd(), 'src/style.css')
    const overrideCss = readFileSync(overridePath, 'utf8')
    const styleCss = readFileSync(stylePath, 'utf8')

    const tokens = tokenNamesFromCss(overrideCss)
    expect(tokens.length).toBeGreaterThan(0)

    for (const token of tokens) {
      expect(
        tokenDefinedInStyleCss(styleCss, token),
        `token "${token}" used in json-viewer.css is not defined in style.css`,
      ).toBe(true)
    }
  })

  it('truncates long string values by default and expands to the full value', async () => {
    const wrapper = mountLong()

    const truncated = wrapper.find('[data-testid="json-viewer-string-truncated"]')
    expect(truncated.exists()).toBe(true)
    expect(truncated.text()).toContain('Expand')
    expect(truncated.text()).toContain('chars')
    expect(truncated.text()).not.toContain(longValue)

    await wrapper.get('[data-testid="json-viewer-string-expand"]').trigger('click')
    await nextTick()

    const expanded = wrapper.find('[data-testid="json-viewer-string-expanded"]')
    expect(expanded.exists()).toBe(true)
    expect(expanded.text()).toContain(longValue)
    expect(wrapper.find('[data-testid="json-viewer-string-truncated"]').exists()).toBe(false)

    await wrapper.get('[data-testid="json-viewer-string-collapse"]').trigger('click')
    await nextTick()

    expect(wrapper.find('[data-testid="json-viewer-string-truncated"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="json-viewer-string-expanded"]').exists()).toBe(false)
  })

  it('truncates a long top-level plain string with an expand affordance', async () => {
    const wrapper = mountPlainLong()

    const truncated = wrapper.find('[data-testid="json-viewer-string-truncated"]')
    expect(truncated.exists()).toBe(true)
    expect(truncated.text()).not.toContain(longValue)

    await wrapper.get('[data-testid="json-viewer-string-expand"]').trigger('click')
    await nextTick()

    expect(wrapper.find('.json-viewer pre').text()).toContain(longValue)
  })

  it('short string values are not truncated', () => {
    const wrapper = mountSimple()
    expect(wrapper.find('[data-testid="json-viewer-string-truncated"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="json-viewer-string-expand"]').exists()).toBe(false)
  })

  it('copy writes the FULL value to the clipboard even when a long string is truncated', async () => {
    const writeText = stubClipboard()
    const wrapper = mountLong()

    await wrapper.get('[data-testid="json-viewer-copy"]').trigger('click')

    expect(writeText).toHaveBeenCalledTimes(1)
    const payload = writeText.mock.calls[0][0] as string
    expect(payload).toContain(longValue)
    expect(payload).not.toContain('…')
  })
})
