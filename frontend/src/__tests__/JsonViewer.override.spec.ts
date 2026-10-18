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

// FAR-626 wrapping case: an unbreakable token below the truncation threshold
// (default dep path) and a spaced sentence long enough to wrap its preview.
const unbreakableToken = 'x'.repeat(400)
const wrapFixture = { session_blob: unbreakableToken, note: 'word '.repeat(60) }

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

function mountWrap() {
  return mount(JsonViewer, { props: { data: wrapFixture } })
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

/**
 * Every style rule reachable from `rules` by descending through grouping rules
 * (`CSSMediaRule`, `CSSSupportsRule`): a grouping rule's children are walked
 * recursively. Style rules nested inside another style rule (CSS nesting) are
 * not traversed — this stylesheet uses none. A top-level-only walk cannot see
 * the FAR-1901 declarations, which live inside
 * `@media (min-width: 641px)` — so this recursion is what makes media-wrapped
 * rules tripwire-able by the same selector/declaration assertions as the
 * top-level ones.
 */
function allStyleRules(rules: CSSRuleList): CSSStyleRule[] {
  const found: CSSStyleRule[] = []
  for (let i = 0; i < rules.length; i++) {
    const rule = rules[i]
    if (rule instanceof CSSStyleRule) found.push(rule)
    if (rule instanceof CSSGroupingRule && rule.cssRules.length > 0) {
      found.push(...allStyleRules(rule.cssRules))
    }
  }
  return found
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
    try {
      if (allStyleRules(rules).some((rule) => rule.selectorText.includes('.json-viewer'))) {
        return sheet
      }
    } catch (err) {
      // jsdom can throw on foreign/at-rule boundaries — skip.
      console.warn('Failed to read cssRules', err)
    }
  }
  return null
}

/**
 * A grouping rule's own condition text (e.g. a media query's
 * `(min-width: 641px)`, or a `@supports` condition). jsdom exposes it as
 * `conditionText` on `CSSMediaRule` / `CSSSupportsRule`; older builds only
 * carry the legacy `media.mediaText` for media rules, so both are read. An
 * unrecognised grouping rule contributes an empty string.
 */
function atRuleCondition(rule: CSSGroupingRule): string {
  const { conditionText } = rule as CSSGroupingRule & { conditionText?: string }
  if (typeof conditionText === 'string' && conditionText.length > 0) return conditionText
  const legacy = (rule as unknown as { media?: { mediaText?: string } }).media
  return typeof legacy?.mediaText === 'string' ? legacy.mediaText : ''
}

/**
 * The at-rule condition(s) enclosing every style rule whose selector contains
 * `selectorPart`, outer-most first. The walk pushes each grouping rule's own
 * condition as it descends, so a rule nested in `@media (min-width: 641px)`
 * reports `[['(min-width: 641px)']]` — while the SAME rule hoisted to top
 * level reports `[[]]` (no enclosing at-rule). That difference is what pins
 * the FAR-1901 scoping: hoisting a desktop-only rule to every viewport must
 * fail here, not only in the deploy-time @regression e2e run.
 */
function enclosingAtRuleConditions(sheet: CSSStyleSheet | null, selectorPart: string): string[][] {
  if (!sheet) return []
  const conditions: string[][] = []
  const walk = (rules: CSSRuleList, chain: string[]): void => {
    for (let i = 0; i < rules.length; i++) {
      const rule = rules[i]
      if (rule instanceof CSSStyleRule) {
        if (rule.selectorText.includes(selectorPart)) conditions.push(chain.slice())
      } else if (rule instanceof CSSGroupingRule && rule.cssRules.length > 0) {
        walk(rule.cssRules, [...chain, atRuleCondition(rule)])
      }
    }
  }
  try {
    walk(sheet.cssRules, [])
  } catch (err) {
    // jsdom can throw on foreign/at-rule boundaries — skip.
    console.warn('Failed to read cssRules', err)
  }
  return conditions
}

function ruleSelectors(sheet: CSSStyleSheet | null): string[] {
  if (!sheet) return []
  const selectors: string[] = []
  try {
    for (const rule of allStyleRules(sheet.cssRules)) selectors.push(rule.selectorText)
  } catch (err) {
    // jsdom can throw on foreign/at-rule boundaries — skip.
    console.warn('Failed to read cssRules', err)
  }
  return selectors
}

/** Values declared for `prop` by every rule whose selector contains `selectorPart`. */
function ruleDeclValues(sheet: CSSStyleSheet | null, selectorPart: string, prop: string): string[] {
  if (!sheet) return []
  const values: string[] = []
  try {
    for (const rule of allStyleRules(sheet.cssRules)) {
      if (rule.selectorText.includes(selectorPart)) {
        values.push(rule.style.getPropertyValue(prop).trim())
      }
    }
  } catch (err) {
    // jsdom can throw on foreign/at-rule boundaries — skip.
    console.warn('Failed to read cssRules', err)
  }
  return values
}

function isZero(value: string): boolean {
  return value !== '' && Number.parseFloat(value) === 0
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

  it('renders a long unbreakable value on the default dep path (FAR-626 wrap case)', () => {
    const wrapper = mountWrap()
    // Below the truncation threshold: the dep's default value rendering stays.
    expect(wrapper.find('[data-testid="json-viewer-string-truncated"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="json-viewer-string-expand"]').exists()).toBe(false)
    const values = wrapper.findAll('.vjs-value-string').map((node) => node.text())
    expect(values.some((text) => text.includes(unbreakableToken))).toBe(true)
    // Tripwire: the wrap CSS targets the dep's classless value-wrapper <span>
    // (the only classless direct child of a row). If a vue-json-pretty upgrade
    // classes or removes it, fail here instead of silently dropping the fix.
    expect(wrapper.find('.vjs-tree-node > span:not([class])').exists()).toBe(true)
  })

  it('keeps the wrap contract in the override stylesheet (FAR-626)', () => {
    mountSimple()
    mountWrap()
    const sheet = findOverrideSheet()
    expect(sheet, 'expected our json-viewer override stylesheet to be mounted').not.toBeNull()

    // Values must be breakable anywhere so a long unbreakable token cannot
    // inflate the row's min-content, and the dep's anonymous value-wrapper
    // flex item (the classless <span> child of .vjs-tree-node) must be free
    // to shrink to the space left of the indent column.
    expect(ruleSelectors(sheet)).toContain('.json-viewer .vjs-value')
    expect(ruleDeclValues(sheet, '.vjs-value', 'overflow-wrap')).toContain('anywhere')
    const wrapperMinWidths = ruleDeclValues(sheet, ':not([class])', 'min-width')
    expect(wrapperMinWidths.some(isZero), `expected a min-width: 0 rule for the value wrapper, got ${JSON.stringify(wrapperMinWidths)}`).toBe(true)

    // When the truncated preview wraps, it must absorb the squeeze itself…
    const textMinWidths = ruleDeclValues(sheet, '.json-viewer-string-truncated-text', 'min-width')
    expect(textMinWidths.some(isZero), `expected min-width: 0 on the preview text, got ${JSON.stringify(textMinWidths)}`).toBe(true)
    // …while the expand toggle and char count keep their natural size.
    for (const part of ['.json-viewer-string-toggle', '.json-viewer-string-count']) {
      expect(ruleDeclValues(sheet, part, 'flex-shrink')).toContain('0')
      expect(ruleDeclValues(sheet, part, 'white-space')).toContain('nowrap')
    }
  })

  it('sees declarations nested inside @media blocks (FAR-1901 desktop rules)', () => {
    mountSimple()
    const sheet = findOverrideSheet()
    expect(sheet, 'expected our json-viewer override stylesheet to be mounted').not.toBeNull()

    // FAR-1901: these two declarations live ONLY inside the desktop
    // `@media (min-width: 641px)` block. The top-level-only walk this probe
    // used before FAR-1923 could not see them, so a deletion or alteration
    // was caught only by the deploy-time @regression e2e run. These
    // assertions fail if the media-rule recursion is removed.
    const commaSelector =
      '.json-viewer .vjs-tree:not(.is-virtual) .vjs-tree-node > span:not([class]) > .vjs-value > .json-viewer-string-truncated'
    const commaLineSelector =
      '.json-viewer .vjs-tree:not(.is-virtual) .vjs-tree-node > span:not([class]) > span:not([class])'

    expect(ruleSelectors(sheet)).toContain(commaSelector)
    expect(ruleSelectors(sheet)).toContain(commaLineSelector)
    expect(
      ruleDeclValues(sheet, '.vjs-value > .json-viewer-string-truncated', 'max-width'),
    ).toEqual(['calc(100% - 1ch)'])
    expect(
      ruleDeclValues(sheet, ':not([class]) > span:not([class])', 'vertical-align'),
    ).toEqual(['bottom'])

    // FAR-1923: pin the SCOPING, not just the contents. Both FAR-1901 rules
    // live inside `@media (min-width: 641px)`; hoisting either to top level
    // would widen the desktop-only rule to every viewport and collide with
    // the FAR-1840 `<=640px` layout. A top-level rule has no enclosing
    // at-rule and reports `[[]]`, so the scoping regression fails here.
    expect(
      enclosingAtRuleConditions(sheet, '.vjs-value > .json-viewer-string-truncated'),
      'FAR-1901 max-width rule must stay scoped to @media (min-width: 641px)',
    ).toEqual([['(min-width: 641px)']])
    expect(
      enclosingAtRuleConditions(sheet, ':not([class]) > span:not([class])'),
      'FAR-1901 vertical-align rule must stay scoped to @media (min-width: 641px)',
    ).toEqual([['(min-width: 641px)']])
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
