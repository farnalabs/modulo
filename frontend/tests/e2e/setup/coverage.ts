import { createRequire } from 'node:module'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import type { Page, TestInfo } from '@playwright/test'

const require = createRequire(import.meta.url)

const COVERAGE_DIR = resolve(import.meta.dirname ?? __dirname, '../../../.coverage/e2e')
const COVERAGE_RAW_DIR = join(COVERAGE_DIR, 'raw')
const REPORT_DIR = join(COVERAGE_DIR, 'report')

interface V8CoverageEntry {
  url: string
  source: string
  ranges: { start: number; end: number }[]
  functions: { functionName: string; ranges: { start: number; end: number }[]; isBlockCoverage: boolean }[]
}

const allCoverageEntries: V8CoverageEntry[] = []

export async function startCoverage(page: Page): Promise<void> {
  await page.coverage.startJSCoverage({ resetOnNavigation: true })
}

export async function stopCoverage(page: Page, testInfo: TestInfo): Promise<void> {
  const entries = await page.coverage.stopJSCoverage()
  for (const entry of entries) {
    if (entry.url.includes('/src/') && !entry.url.includes('node_modules')) {
      allCoverageEntries.push({
        url: entry.url,
        source: entry.source,
        ranges: entry.ranges,
        functions: entry.functions ?? [],
      })
    }
  }
}

export async function generateCoverageReport(): Promise<string> {
  if (!existsSync(COVERAGE_RAW_DIR)) {
    mkdirSync(COVERAGE_RAW_DIR, { recursive: true })
  }

  for (let i = 0; i < allCoverageEntries.length; i++) {
    const entry = allCoverageEntries[i]
    const filename = `coverage-${i}.json`
    writeFileSync(join(COVERAGE_RAW_DIR, filename), JSON.stringify(entry, null, 2))
  }

  if (allCoverageEntries.length === 0) {
    return '0.0'
  }

  const { default: V8ToIstanbul } = await import('v8-to-istanbul')
  const { createReporter, Report } = require('istanbul-reports')

  const coverageMap: Record<string, any> = {}

  for (const entry of allCoverageEntries) {
    try {
      const script = V8ToIstanbul(entry.url)
      await script.load()
      script.applyCoverage(entry.functions)
      const istanbulData = script.toIstanbul()
      Object.assign(coverageMap, istanbulData)
    } catch {
      // Skip files that can't be converted (non-JS, inline scripts, etc.)
    }
  }

  if (Object.keys(coverageMap).length === 0) {
    return '0.0'
  }

  if (!existsSync(REPORT_DIR)) {
    mkdirSync(REPORT_DIR, { recursive: true })
  }
  writeFileSync(join(COVERAGE_DIR, 'coverage-final.json'), JSON.stringify(coverageMap, null, 2))

  const { Watermarks } = require('istanbul-lib-report')
  const libReport = require('istanbul-lib-report')
  const reports = require('istanbul-reports')
  const libCoverage = require('istanbul-lib-coverage')
  const libSourceMaps = require('istanbul-lib-source-maps')

  const coverageVar = libCoverage.createCoverageMap(coverageMap)
  const sourceMapStore = libSourceMaps.createSourceMapStore()
  const mapped = await sourceMapStore.transformCoverage(coverageVar)

  const context = libReport.createContext({
    dir: REPORT_DIR,
    coverageMap: mapped,
    watermarks: Watermarks.getDefault(),
  })

  reports.create('lcovonly', {}).execute(context)
  reports.create('text-summary', {}).execute(context)
  reports.create('html', {}).execute(context)

  const textSummary = reports.create('text-summary', {})
  let result = ''
  const origWrite = process.stdout.write
  process.stdout.write = (chunk: any) => { result += chunk.toString(); return true }
  textSummary.execute(context)
  process.stdout.write = origWrite

  const match = result.match(/Lines\s*:\s*([\d.]+)%/)
  return match ? match[1] : '0.0'
}
