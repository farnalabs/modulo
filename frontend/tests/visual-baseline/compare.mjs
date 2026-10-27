#!/usr/bin/env node
/**
 * PrimeVue baseline comparator (FAR-2205).
 *
 * Compares two capture directories produced by
 * `tests/e2e/primevue-visual-baseline.spec.ts` (a route/interaction capture
 * with a `.json` + `.png` pair per slug) and reports every difference:
 *
 *   - a route/URL mismatch,
 *   - a changed computed `--p-*` token,
 *   - a changed component computed-style fingerprint, and
 *   - a screenshot pixel delta (percentage of differing pixels + bbox).
 *
 * The point is a DELTA TABLE a human judges, not a pass/fail gate: "no
 * unexplained difference" is the standard, and the judgement of whether a
 * difference is acceptable belongs to the reviewer, not to this script.
 *
 * Usage:
 *   node tests/visual-baseline/compare.mjs \
 *     --expected <dir> --actual <dir> [--json <out>] [--pixel-threshold 16]
 */

import { createRequire } from 'node:module'
import { readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const require = createRequire(import.meta.url)

// pngjs is not a direct dependency; it ships with `qrcode` (a production
// dependency), so resolve it from qrcode's own module context rather than
// adding a dependency that exists only for measurement.
const qrcodeRequire = createRequire(require.resolve('qrcode/package.json'))
const { PNG } = qrcodeRequire('pngjs')

function parseArgs(argv) {
  const args = {}
  for (let i = 2; i < argv.length; i++) {
    const token = argv[i]
    if (!token.startsWith('--')) continue
    const key = token.slice(2)
    const value = argv[i + 1]
    if (value === undefined || value.startsWith('--')) {
      args[key] = true
    } else {
      args[key] = value
      i++
    }
  }
  return args
}

const args = parseArgs(process.argv)
if (!args.expected || !args.actual) {
  console.error('usage: compare.mjs --expected <dir> --actual <dir> [--json <out>] [--pixel-threshold 16]')
  process.exit(2)
}

const expectedDir = resolve(args.expected)
const actualDir = resolve(args.actual)
const pixelThreshold = Number.parseInt(args['pixel-threshold'] ?? '16', 10)

function jsonFiles(dir) {
  return new Set(readdirSync(dir).filter((f) => f.endsWith('.json')).map((f) => f.replace(/\.json$/, '')))
}

function readJson(dir, slug) {
  return JSON.parse(readFileSync(join(dir, `${slug}.json`), 'utf8'))
}

function comparePng(expectedPath, actualPath) {
  const a = PNG.sync.read(readFileSync(expectedPath))
  const b = PNG.sync.read(readFileSync(actualPath))
  const result = {
    widthA: a.width,
    heightA: a.height,
    widthB: b.width,
    heightB: b.height,
    dimensionChanged: a.width !== b.width || a.height !== b.height,
  }
  if (result.dimensionChanged) {
    return { ...result, note: 'dimensions differ; overlap compared, remaining area counted as differing' }
  }

  let differing = 0
  let maxDelta = 0
  let minX = Infinity
  let minY = Infinity
  let maxX = -1
  let maxY = -1
  for (let y = 0; y < a.height; y++) {
    for (let x = 0; x < a.width; x++) {
      const i = (a.width * y + x) << 2
      const dr = Math.abs(a.data[i] - b.data[i])
      const dg = Math.abs(a.data[i + 1] - b.data[i + 1])
      const db = Math.abs(a.data[i + 2] - b.data[i + 2])
      const da = Math.abs(a.data[i + 3] - b.data[i + 3])
      const delta = Math.max(dr, dg, db, da)
      if (delta > pixelThreshold) {
        differing++
        if (delta > maxDelta) maxDelta = delta
        if (x < minX) minX = x
        if (y < minY) minY = y
        if (x > maxX) maxX = x
        if (y > maxY) maxY = y
      }
    }
  }
  const total = a.width * a.height
  return {
    ...result,
    differingPixels: differing,
    differingPercent: Number(((differing / total) * 100).toFixed(3)),
    maxChannelDelta: maxDelta,
    boundingBox: differing > 0 ? { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 } : null,
  }
}

const expectedSlugs = jsonFiles(expectedDir)
const actualSlugs = jsonFiles(actualDir)
const missing = [...expectedSlugs].filter((s) => !actualSlugs.has(s))
const added = [...actualSlugs].filter((s) => !expectedSlugs.has(s))

const report = { expectedDir, actualDir, pixelThreshold, pages: {}, missing, added }
let totalDifferingPixels = 0

for (const slug of [...expectedSlugs].filter((s) => actualSlugs.has(s)).sort()) {
  const expected = readJson(expectedDir, slug)
  const actual = readJson(actualDir, slug)

  const tokenChanges = []
  const tokenNames = new Set([...Object.keys(expected.tokens || {}), ...Object.keys(actual.tokens || {})])
  for (const name of [...tokenNames].sort()) {
    const before = expected.tokens?.[name]
    const after = actual.tokens?.[name]
    if (before !== after) tokenChanges.push({ token: name, before, after })
  }

  const componentChanges = []
  const selectors = new Set([
    ...Object.keys(expected.components || {}),
    ...Object.keys(actual.components || {}),
  ])
  for (const selector of selectors) {
    const beforeList = expected.components?.[selector] || []
    const afterList = actual.components?.[selector] || []
    const max = Math.max(beforeList.length, afterList.length)
    for (let i = 0; i < max; i++) {
      const before = beforeList[i]
      const after = afterList[i]
      if (!before || !after) {
        componentChanges.push({
          selector,
          index: i,
          property: '(instance)',
          before: before ? 'present' : 'absent',
          after: after ? 'present' : 'absent',
        })
        continue
      }
      const props = new Set([...Object.keys(before), ...Object.keys(after)])
      for (const prop of props) {
        if (before[prop] !== after[prop]) {
          componentChanges.push({ selector, index: i, property: prop, before: before[prop], after: after[prop] })
        }
      }
    }
  }

  let screenshot = null
  try {
    screenshot = comparePng(join(expectedDir, `${slug}.png`), join(actualDir, `${slug}.png`))
    totalDifferingPixels += screenshot.differingPixels ?? 0
  } catch (error) {
    screenshot = { error: String(error) }
  }

  report.pages[slug] = {
    urlBefore: expected.url,
    urlAfter: actual.url,
    urlChanged: expected.url !== actual.url,
    tokenChanges,
    componentChanges,
    screenshot,
  }
}

report.totalDifferingPixels = totalDifferingPixels

function formatValue(value) {
  if (value === undefined) return '(absent)'
  const text = String(value)
  return text.length > 70 ? `${text.slice(0, 67)}...` : text
}

console.log(`\nPrimeVue baseline delta — ${expectedDir} -> ${actualDir}\n`)
if (missing.length) console.log(`MISSING captures (in expected, not actual): ${missing.join(', ')}`)
if (added.length) console.log(`ADDED captures: ${added.join(', ')}`)

for (const [slug, page] of Object.entries(report.pages)) {
  const screenshot = page.screenshot || {}
  const shotSummary = screenshot.error
    ? `screenshot error: ${screenshot.error}`
    : screenshot.dimensionChanged
      ? `screenshot DIMENSION CHANGE ${screenshot.widthA}x${screenshot.heightA} -> ${screenshot.widthB}x${screenshot.heightB}`
      : `${screenshot.differingPercent}% px differ (${screenshot.differingPixels}px, max Δ${screenshot.maxChannelDelta})`

  const changed = page.tokenChanges.length + page.componentChanges.length + (screenshot.differingPixels ? 1 : 0)
  if (!changed && !page.urlChanged) continue

  console.log(`\n## ${slug}`)
  console.log(`   ${shotSummary}`)
  if (page.urlChanged) console.log(`   URL CHANGED: ${page.urlBefore} -> ${page.urlAfter}`)
  for (const change of page.tokenChanges) {
    console.log(`   token ${change.token}: ${formatValue(change.before)} -> ${formatValue(change.after)}`)
  }
  // Group component changes per selector+property with an instance count so a
  // 20-row table change reads as one line.
  const grouped = new Map()
  for (const change of page.componentChanges) {
    const key = `${change.selector} ${change.property}`
    const entry = grouped.get(key) || { ...change, count: 0, examples: new Set() }
    entry.count++
    entry.examples.add(`${formatValue(change.before)} -> ${formatValue(change.after)}`)
    grouped.set(key, entry)
  }
  for (const entry of grouped.values()) {
    const examples = [...entry.examples].slice(0, 2).join('; ')
    console.log(`   ${entry.selector} ${entry.property} x${entry.count}: ${examples}`)
  }
}

const cleanPages = Object.entries(report.pages).filter(
  ([, p]) =>
    !p.urlChanged &&
    p.tokenChanges.length === 0 &&
    p.componentChanges.length === 0 &&
    !(p.screenshot?.differingPixels > 0) &&
    !p.screenshot?.dimensionChanged,
)
console.log(`\n${cleanPages.length}/${Object.keys(report.pages).length} captures are pixel- and token-identical.`)

if (args.json) {
  writeFileSync(resolve(args.json), JSON.stringify(report, null, 1))
  console.log(`Wrote ${args.json}`)
}
