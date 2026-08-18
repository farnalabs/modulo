import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import Aura from '@primeuix/themes/aura'
import { toNormalizeVariable, toVariables } from '@primeuix/styled'
import {
  PRIMEVUE_TOKEN_BRIDGE,
  applyPrimeVueTokenBridge,
  primeVueTokenName,
  sourceTokenName,
} from '../lib/primevue-theme'

/**
 * Guard test for the PrimeVue token bridge.
 *
 * The token bridge (`src/lib/primevue-theme.ts`) is the single source of truth
 * mapping our semantic CSS vars to PrimeVue's `--p-*` tokens. This test fails
 * CI the instant either side of the mapping breaks:
 *
 *  1. Every mapped `--p-*` target must be a REAL token that the Aura preset
 *     actually defines (a renamed/removed PrimeVue token breaks the bridge).
 *  2. Every referenced source CSS variable must be actually defined in
 *     `src/style.css` (a renamed/removed theme token breaks the bridge).
 *
 * This is the instant-regression catch required by ADR 024 — the token bridge
 * is owned by a named owner and guarded here so regressions are caught at
 * commit/CI time, never at runtime.
 */

/** The set of `--p-*` CSS variable names the Aura preset defines. */
function auraTokenSet(): Set<string> {
  // toVariables returns dotted token keys (e.g. `primary.color`); the actual
  // CSS variable name normalises dots to dashes (`--p-primary-color`).
  const { tokens } = toVariables(
    Aura as unknown as Parameters<typeof toVariables>[0],
    { prefix: 'p' },
  )
  return new Set((tokens || []).map((t) => `--p-${toNormalizeVariable(t)}`))
}

/** The set of CSS custom property names declared in style.css. */
function definedCssVars(): Set<string> {
  // Read the source file from disk (not a bundled `?raw` import, which Vite
  // returns empty here) so the guard checks the real on-disk token set.
  const stylePath = resolve(process.cwd(), 'src/style.css')
  const css = readFileSync(stylePath, 'utf8')
  const vars = new Set<string>()
  const re = /(--[\w-]+)\s*:/g
  let m: RegExpExecArray | null
  while ((m = re.exec(css)) !== null) {
    vars.add(m[1])
  }
  return vars
}

describe('PrimeVue token bridge guard', () => {
  it('every mapped --p-* target is a real token in the Aura preset', () => {
    const auraTokens = auraTokenSet()
    const missing = PRIMEVUE_TOKEN_BRIDGE.filter(
      (entry) => !auraTokens.has(primeVueTokenName(entry)),
    )
    expect(missing).toEqual([])
  })

  it('every referenced source CSS variable is defined in style.css', () => {
    const defined = definedCssVars()
    const missing = PRIMEVUE_TOKEN_BRIDGE.filter(
      (entry) => !defined.has(sourceTokenName(entry)),
    )
    expect(missing).toEqual([])
  })

  it('applies the bridge as hsl(var(--source)) values on the document root', () => {
    // Guard that the mapping entries are non-trivial (real work exists) and
    // that the source names are unique (no duplicate mappings that would make
    // one source silently override another).
    expect(PRIMEVUE_TOKEN_BRIDGE.length).toBeGreaterThan(10)
    const sources = PRIMEVUE_TOKEN_BRIDGE.map((e) => e.source)
    expect(new Set(sources).size).toBe(sources.length)

    // Actually invoke the bridge on a real element and assert the DOM mapping,
    // so the central deliverable of ADR 024 Decision 4 is proven to do what it
    // claims: every target `--p-*` variable written as `hsl(var(--<source>))`.
    // Several sources intentionally share a target (e.g. card/popover → surface),
    // so later entries override earlier ones for the same `--p-*` variable —
    // mirror that loop to compute the expected DOM, exactly as the bridge runs.
    const root = document.createElement('div')
    applyPrimeVueTokenBridge(root)
    const expected = new Map<string, string>()
    for (const entry of PRIMEVUE_TOKEN_BRIDGE) {
      expected.set(entry.target, `hsl(var(${sourceTokenName(entry)}))`)
    }
    for (const [target, value] of expected) {
      expect(root.style.getPropertyValue(`--p-${target}`)).toBe(value)
    }
    // Spot-check one full mapping for readability of failures.
    expect(root.style.getPropertyValue('--p-primary-color')).toBe('hsl(var(--primary))')
  })
})
