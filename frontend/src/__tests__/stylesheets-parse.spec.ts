/**
 * Gate: every stylesheet and every Vue `<style>` block must parse.
 *
 * Why this exists (FAR-1995): during FAR-1938 a bulk edit produced
 * `background-color: hsl(var(--primary);` (an unclosed paren) in 16
 * declarations. ESLint does not parse stylesheets, `vue-tsc` does not
 * typecheck them, and Vitest stubs CSS imports — so the entire suite stayed
 * green and the broken sheet would have reached `main` and shipped an
 * unstyled page. The only reason it was caught was that the reporter was
 * required to fetch the module from a dev server, where PostCSS reported
 * `Unclosed bracket`.
 *
 * This spec closes that gap where frontend PRs already run:
 * `pnpm run test:unit` (and the frontend CI job's `vitest run src`) discover
 * it, so a parse error fails the local gate and CI with file, line, column
 * and PostCSS's message.
 *
 * It parses with PostCSS's core syntax parser and `@vue/compiler-sfc` — both
 * already direct dependencies (no new dependency). Core PostCSS parsing is
 * syntax-only: it accepts arbitrary at-rules, so Tailwind directives
 * (`@tailwind`, `@apply`, `@layer`, `@theme`, `@utility`, `@config`,
 * `@reference`, `@custom-media`, ...) parse cleanly. The final test pins that
 * no-false-positive contract against a fixture containing them.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { extname, join, relative, resolve } from 'node:path'
import { parse as parseCss } from 'postcss'
import { parse as parseSfc } from '@vue/compiler-sfc'

const FRONTEND_ROOT = resolve(__dirname, '../..')
const SRC_ROOT = resolve(FRONTEND_ROOT, 'src')

interface ParseFailure {
  /** Absolute path of the stylesheet or `.vue` file. */
  file: string
  /** Index of the offending `<style>` block, or null for a standalone sheet. */
  blockIndex: number | null
  /** Absolute 1-based line in `file`. */
  line: number
  /** Absolute 1-based column in `file`. */
  column: number
  /** PostCSS's short reason (e.g. `Unclosed bracket`). */
  reason: string
  /** The offending source line, for the failure report. */
  sourceLine: string | null
}

interface CssGateResult {
  cssFiles: string[]
  styleBlocks: number
  failures: ParseFailure[]
}

interface PostcssError extends Error {
  reason?: string
  line?: number
  column?: number
}

function walkFiles(dir: string): string[] {
  const files: string[] = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      files.push(...walkFiles(full))
    } else {
      files.push(full)
    }
  }
  return files.sort()
}

/**
 * Parse one CSS source. `contentStartLine` / `contentStartColumn` are the
 * absolute position of the source's first character, so an error inside a
 * `<style>` block is reported at the line/column a reader can find in the
 * `.vue` file rather than relative to the block.
 */
function parseSource(
  source: string,
  file: string,
  object: { blockIndex?: number | null; contentStartLine?: number; contentStartColumn?: number } = {},
): ParseFailure | null {
  const {
    blockIndex = null,
    contentStartLine = 1,
    contentStartColumn = 1,
  } = object

  try {
    parseCss(source, { from: file })
    return null
  } catch (error) {
    const postcssError = error as PostcssError
    const relativeLine = typeof postcssError.line === 'number' ? postcssError.line : 1
    const relativeColumn = typeof postcssError.column === 'number' ? postcssError.column : 1
    const line = contentStartLine + relativeLine - 1
    const column = relativeLine === 1 ? contentStartColumn + relativeColumn - 1 : relativeColumn
    const sourceLines = source.split(/\r?\n/)
    return {
      file,
      blockIndex,
      line,
      column,
      reason: postcssError.reason ?? postcssError.message,
      sourceLine: sourceLines[relativeLine - 1] ?? null,
    }
  }
}

function collectAndParse(root: string): CssGateResult {
  const cssFiles: string[] = []
  let styleBlocks = 0
  const failures: ParseFailure[] = []

  for (const file of walkFiles(root)) {
    const extension = extname(file)
    if (extension === '.css') {
      cssFiles.push(file)
      const failure = parseSource(readFileSync(file, 'utf8'), file)
      if (failure) failures.push(failure)
      continue
    }
    if (extension !== '.vue') continue

    const source = readFileSync(file, 'utf8')
    const { descriptor } = parseSfc(source, { filename: file })
    descriptor.styles.forEach((block, index) => {
      styleBlocks += 1
      const failure = parseSource(block.content, file, {
        blockIndex: index,
        contentStartLine: block.loc.start.line,
        contentStartColumn: block.loc.start.column,
      })
      if (failure) failures.push(failure)
    })
  }

  return { cssFiles, styleBlocks, failures }
}

function formatFailure(failure: ParseFailure): string {
  const location = `${relative(FRONTEND_ROOT, failure.file)}:${failure.line}:${failure.column}`
  const block = failure.blockIndex === null ? '' : ` (Vue <style> block #${failure.blockIndex + 1})`
  const lines = [`${location}${block} — ${failure.reason}`]
  if (failure.sourceLine !== null) {
    lines.push(`    ${failure.sourceLine}`)
    lines.push(`    ${' '.repeat(Math.max(failure.column - 1, 0))}^`)
  }
  return lines.join('\n')
}

function formatFailures(failures: ParseFailure[]): string {
  return failures.map(formatFailure).join('\n\n')
}

describe('CSS parse gate (FAR-1995)', () => {
  let fixtureRoot: string

  beforeEach(() => {
    fixtureRoot = mkdtempSync(join(tmpdir(), 'modulo-css-parse-gate-'))
  })

  afterEach(() => {
    rmSync(fixtureRoot, { recursive: true, force: true })
  })

  it('parses every stylesheet and component <style> block under src/', () => {
    const result = collectAndParse(SRC_ROOT)

    // The real gate: an unclosed bracket anywhere reds this with a report.
    expect(formatFailures(result.failures)).toBe('')

    // Non-vacuity: prove the walker actually inspected the real surface, so
    // this test cannot pass by finding nothing.
    expect(result.cssFiles.map((file) => relative(SRC_ROOT, file))).toContain('style.css')
    expect(result.styleBlocks).toBeGreaterThan(0)
  })

  it('fails on an unclosed paren, naming the file, line and PostCSS message', () => {
    writeFileSync(
      join(fixtureRoot, 'broken.css'),
      [
        '.broken {',
        '  color: red;',
        '  background-color: hsl(var(--primary);',
        '}',
        '',
      ].join('\n'),
    )

    const result = collectAndParse(fixtureRoot)

    expect(result.failures).toHaveLength(1)
    const report = formatFailures(result.failures)
    expect(report).toContain('broken.css:3:24')
    expect(report).toContain('Unclosed bracket')
    expect(report).toContain('background-color: hsl(var(--primary);')
  })

  it('fails inside a component <style> block and reports the .vue line', () => {
    writeFileSync(
      join(fixtureRoot, 'Broken.vue'),
      [
        '<template>',
        '  <div />',
        '</template>',
        '',
        '<style scoped>',
        '.broken {',
        '  background-color: hsl(var(--primary);',
        '}',
        '</style>',
        '',
      ].join('\n'),
    )

    const result = collectAndParse(fixtureRoot)

    expect(result.failures).toHaveLength(1)
    const report = formatFailures(result.failures)
    expect(report).toContain('Broken.vue:7:')
    expect(report).toContain('Vue <style> block #1')
    expect(report).toContain('Unclosed bracket')
  })

  it('accepts Tailwind directives and custom at-rules without false positives', () => {
    writeFileSync(
      join(fixtureRoot, 'tailwind.css'),
      [
        '@import "tailwindcss";',
        '@config "../tailwind.config.cjs";',
        '',
        '@tailwind base;',
        '@tailwind components;',
        '@tailwind utilities;',
        '',
        '@theme {',
        '  --color-brand: hsl(var(--primary));',
        '}',
        '',
        '@layer components {',
        '  .btn {',
        '    @apply inline-flex items-center rounded-md px-3 py-2;',
        '  }',
        '}',
        '',
        '@utility tab-4 {',
        '  tab-size: 4;',
        '}',
        '',
        '@custom-media --narrow (max-width: 30em);',
        '@container (min-width: 40rem) {',
        '  .card {',
        '    color: red;',
        '  }',
        '}',
        '',
      ].join('\n'),
    )
    writeFileSync(
      join(fixtureRoot, 'TailwindComponent.vue'),
      [
        '<template>',
        '  <div class="btn" />',
        '</template>',
        '',
        '<style scoped>',
        '@reference "tailwindcss";',
        '.btn {',
        '  @apply inline-flex items-center gap-2;',
        '}',
        '@media (hover: hover) {',
        '  .btn:hover {',
        '    color: red;',
        '  }',
        '}',
        '</style>',
        '',
      ].join('\n'),
    )

    const result = collectAndParse(fixtureRoot)

    expect(formatFailures(result.failures)).toBe('')
    expect(result.cssFiles).toHaveLength(1)
    expect(result.styleBlocks).toBe(1)
  })
})
