#!/usr/bin/env node
/**
 * FAR-2017 — the visual/token e2e slice that runs on every frontend PR.
 *
 * Why this exists
 * ---------------
 * The staging `@regression` gate was the ONLY job running the Playwright
 * specs that assert rendered CSS, design tokens and layout. A token change
 * therefore passed every PR check, landed on `main`, and only went red
 * post-deploy — blocking the deploy train at the most expensive moment
 * (FAR-2016: `json-viewer.spec.ts` light-mode token probe; FAR-1913/2028:
 * `mobile-layout.spec.ts` axe `color-contrast`).
 *
 * The whole suite is not affordable per PR (~313 tests, ~11 min against a
 * deployed target, plus provisioning). This module selects a narrow,
 * unconditional slice of the token/CSS/geometry specs that are cheap and
 * deterministic against the LOCAL target, so they run on every frontend PR.
 *
 * Why unconditional rather than changed-path based
 * ------------------------------------------------
 * A path filter is fragile in exactly the direction that matters here: it
 * can miss a spec whose subject changed without a matching path (a token
 * defined in JS, a class rename inside a `.vue` script block, a shared
 * component whose rendered geometry changed). A selector that under-selects
 * is worse than none, because it reads as coverage. The slice is small
 * enough (~2 min) that gating it on a path heuristic buys little.
 *
 * Completeness guard
 * ------------------
 * `--check` scans every `@regression` spec for visual-assertion markers. A
 * marked spec MUST be either in SLICE (runs on every PR) or in
 * STAGING_ONLY (deliberately not on every PR, with a written reason). A new
 * visual spec that is in neither fails the PR with instructions, so the
 * slice cannot silently rot into under-selection.
 *
 * Usage (from `frontend/`):
 *   node scripts/e2e-visual-slice.mjs --check   # guard (CI step)
 *   node scripts/e2e-visual-slice.mjs --list    # space-separated spec paths
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(HERE, '..')
const E2E_ROOT = join(FRONTEND_ROOT, 'tests', 'e2e')

/**
 * Specs that run on every frontend PR. Each entry is here because it asserts
 * rendered CSS, design tokens or layout, runs against the local mock target,
 * and is cheap/deterministic enough for a per-PR gate.
 */
const SLICE = [
  // FAR-2016 instance: design-token colours (light-mode probe) + long-value
  // geometry. The exact spec whose stale probe blocked the deploy train.
  'tests/e2e/json-viewer.spec.ts',
  // WCAG AA (axe) audit — the contrast/token class. Misleadingly named: its
  // describe is "WCAG AA audit (local)" and it runs in full against the local
  // target, not staging.
  'tests/e2e/staging-wcag.spec.ts',
  // Overlay geometry: a dropdown that is clipped, mis-anchored or rendered at
  // the viewport edge is a layout regression invisible to unit/type checks.
  'tests/e2e/filter-bar-overlay.spec.ts',
  'tests/e2e/select-clipping-container.spec.ts',
  'tests/e2e/pipeline-editor-select-overlay.spec.ts',
]

/**
 * Visual-assertion specs deliberately NOT on every PR, each with the reason.
 * These continue to run in the staging `@regression` gate — this list adds
 * nothing and removes nothing; it documents a measured decision so the
 * completeness guard treats it as classified rather than silently missed.
 */
const STAGING_ONLY = {
  'tests/e2e/wcag-contrast.spec.ts':
    'Already runs in this same frontend job as a dedicated, earlier step — not duplicated here.',
  'tests/e2e/mobile-layout.spec.ts':
    'Measured 8.1 min locally (82-route sweep + axe + CLS + screenshots), and its axe findings can need seeded deployment data (the FAR-2028 unread-badge contrast node only renders with unread notifications, which the local mock does not seed). Too expensive and not data-realistic per PR; stays in the staging gate.',
  'tests/e2e/pipeline-editor.spec.ts':
    'Layout-fit assertions interleaved with multi-step editor interactions (12 tests, ~1.1 min); flaked once in a combined local run under host contention. Broader interaction surface than the token/geometry slice, so it stays in the staging gate.',
  'tests/e2e/run-detail-metadata.spec.ts':
    'Layout alignment is cheap alone (~35 s) but depends on run-detail mock/loading sequencing; flaked once in a combined slice run. Left in the staging gate rather than adding a flake source to a required PR check.',
  'tests/e2e/notifications.spec.ts':
    'Bell-dropdown viewport anchoring is layout, but the file is dominated by interaction/state tests (9 tests, ~47 s) that are not the token/CSS offender class. Left in the staging gate.',
  'tests/e2e/dialog-select-overlay.spec.ts':
    'Overlay geometry is in the slice class, but this file also carries a pre-existing local-only keyboard-focus failure ("options stay keyboard-selectable inside the modal" — the combobox never regains focus locally). The failure is unrelated to rendered CSS/tokens and is tracked separately; including the file now would make the required gate permanently red. Left in the staging gate.',
}

/**
 * Markers that identify a spec asserting rendered output rather than data or
 * navigation. Deliberately broad: over-selecting makes the guard ask for a
 * classification, which is cheap; under-selecting is the failure mode it
 * exists to prevent.
 */
const VISUAL_MARKERS = [
  /toHaveCSS/,
  /getComputedStyle/,
  /getBoundingClientRect/,
  /\.boundingBox\s*\(/,
  /toHaveScreenshot/,
  /\.screenshot\s*\(/,
  /toBeInViewport/,
  /var\(--/,
  /setup\/wcag/,
  /\b(?:checkAxe|runAxe|AxeBuilder|axe\.run)\b/,
  /scrollWidth/,
  /clientWidth/,
  /offsetWidth/,
]

const REGRESSION = /@regression/

function toPosix(path) {
  return path.split(sep).join('/')
}

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    const stats = statSync(full)
    if (stats.isDirectory()) out.push(...walk(full))
    else if (entry.endsWith('.spec.ts')) out.push(full)
  }
  return out
}

function classify() {
  const specs = walk(E2E_ROOT)
    .map((full) => ({ path: toPosix(relative(FRONTEND_ROOT, full)), source: readFileSync(full, 'utf8') }))
    .sort((a, b) => a.path.localeCompare(b.path))

  const names = new Set(specs.map((s) => s.path))
  const failures = []

  // Every SLICE file must exist, still be tagged @regression, and still assert
  // rendered output. If a spec stops being visual, its slot should be removed
  // deliberately rather than run forever for no reason.
  for (const path of SLICE) {
    const spec = specs.find((s) => s.path === path)
    if (!spec) {
      failures.push(`${path} is in SLICE but the file does not exist under tests/e2e`)
      continue
    }
    if (!REGRESSION.test(spec.source)) {
      failures.push(`${path} is in SLICE but no longer contains @regression`)
    }
    if (!VISUAL_MARKERS.some((re) => re.test(spec.source))) {
      failures.push(`${path} is in SLICE but no longer matches any visual-assertion marker — remove it or reclassify it`)
    }
  }

  // Every STAGING_ONLY entry must still exist, so the reason cannot outlive
  // the spec it describes.
  for (const path of Object.keys(STAGING_ONLY)) {
    if (!names.has(path)) failures.push(`${path} is in STAGING_ONLY but the file does not exist under tests/e2e`)
  }

  const classified = new Set([...SLICE, ...Object.keys(STAGING_ONLY)])
  const unclassified = specs.filter(
    (spec) =>
      REGRESSION.test(spec.source) &&
      VISUAL_MARKERS.some((re) => re.test(spec.source)) &&
      !classified.has(spec.path),
  )

  for (const spec of unclassified) {
    failures.push(
      `${spec.path} asserts rendered CSS/tokens/layout but is in neither SLICE nor STAGING_ONLY. ` +
        'Add it to SLICE if it is cheap and deterministic against the local target, or to STAGING_ONLY with the reason it cannot run on every PR.',
    )
  }

  return { specs, failures, unclassified }
}

function main() {
  const mode = process.argv[2]
  if (mode !== '--check' && mode !== '--list') {
    process.stderr.write('usage: node scripts/e2e-visual-slice.mjs --check | --list\n')
    process.exit(2)
  }

  const { specs, failures } = classify()

  if (mode === '--list') {
    if (failures.length > 0) {
      process.stderr.write(`[e2e-visual-slice] classification is invalid:\n- ${failures.join('\n- ')}\n`)
      process.exit(1)
    }
    process.stdout.write(`${SLICE.join(' ')}\n`)
    return
  }

  const tagged = specs.filter((s) => REGRESSION.test(s.source)).length
  const visual = specs.filter((s) => REGRESSION.test(s.source) && VISUAL_MARKERS.some((re) => re.test(s.source))).length
  process.stdout.write(
    `[e2e-visual-slice] ${specs.length} spec file(s) scanned, ${tagged} tagged @regression; ${visual} assert rendered output; ` +
      `${SLICE.length} run on every PR; ${Object.keys(STAGING_ONLY).length} explicitly staging-only.\n`,
  )

  if (failures.length > 0) {
    process.stderr.write(`[e2e-visual-slice] FAILED:\n- ${failures.join('\n- ')}\n`)
    process.exit(1)
  }

  for (const path of SLICE) process.stdout.write(`  PR slice: ${path}\n`)
  for (const [path, reason] of Object.entries(STAGING_ONLY)) {
    process.stdout.write(`  staging-only: ${path} — ${reason}\n`)
  }
}

main()
