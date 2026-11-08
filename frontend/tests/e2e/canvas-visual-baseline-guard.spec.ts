import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test } from '@playwright/test'

/**
 * Guard the canvas visual baselines against a vacuous theme axis.
 *
 * The canvas snapshots are captured once per `(state, viewport, theme,
 * platform)`. The light and dark captures exercise different CSS (`:root` dark
 * values vs the `.light` overrides in `style.css`), so for a given
 * `(state, viewport, platform)` the two committed PNGs MUST differ. When the
 * theme was pinned from an init script the parser overwrote it, every "light"
 * capture silently rendered the dark palette, and all 32 baselines pinned the
 * same (dark) render. This spec turns that class of regression — a theme
 * dimension that looks covered but captures nothing — into a hard failure.
 *
 * Runs as part of the `@visual` set (`test:e2e:visual`), so the same CI step
 * that compares the snapshots also proves the pair is meaningful. Reads the
 * committed files directly: no browser, no app.
 */

const SNAPSHOT_DIR = join(process.cwd(), 'tests', 'e2e', 'canvas.visual.spec.ts-snapshots')

// `state` contains hyphens (e.g. `six-node-types`); the viewport/theme/platform
// suffixes are a fixed enumeration, so anchor those and let state be greedy.
const BASELINE = /^canvas-(?<state>.+)-(?<viewport>desktop|mobile)-(?<theme>light|dark)-(?<platform>[a-z0-9]+)\.png$/

interface BaselinePair {
  key: string
  light: string
  dark: string
}

function collectPairs(): BaselinePair[] {
  const files = readdirSync(SNAPSHOT_DIR)
  const grouped = new Map<string, { light?: string; dark?: string }>()
  for (const file of files) {
    const match = BASELINE.exec(file)
    if (!match?.groups) continue
    const { state, viewport, theme, platform } = match.groups
    const key = `${state}-${viewport}-${platform}`
    const entry = grouped.get(key) ?? {}
    entry[theme as 'light' | 'dark'] = file
    grouped.set(key, entry)
  }

  const pairs: BaselinePair[] = []
  for (const [key, entry] of grouped) {
    // A group with only one theme is itself a defect: the pair cannot be
    // verified, so surface it as an explicit failure rather than skipping it.
    expect(entry.light, `missing light baseline for ${key}`).toBeTruthy()
    expect(entry.dark, `missing dark baseline for ${key}`).toBeTruthy()
    pairs.push({ key, light: entry.light!, dark: entry.dark! })
  }
  return pairs
}

test.describe('Canvas visual baselines', { tag: '@visual' }, () => {
  test('every light/dark baseline pair pins a different render', () => {
    const pairs = collectPairs()
    expect(pairs.length, 'no canvas baseline pairs found').toBeGreaterThan(0)

    const identical = pairs.filter((pair) =>
      readFileSync(join(SNAPSHOT_DIR, pair.light)).equals(readFileSync(join(SNAPSHOT_DIR, pair.dark))),
    )

    expect(
      identical.map((pair) => pair.key),
      'light and dark baselines are byte-identical, so the theme axis is vacuous',
    ).toEqual([])
  })
})
