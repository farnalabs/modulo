import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import type { Page } from '@playwright/test'

const COVERAGE_DIR = resolve(import.meta.dirname!, '../../../.coverage/e2e')
const REPORT_DIR = join(COVERAGE_DIR, 'report')

let masterCoverage: Record<string, unknown> = {}

export async function startCoverage(page: Page): Promise<void> {}

export async function stopCoverage(page: Page): Promise<void> {
  try {
    const cov: Record<string, unknown> | undefined = await page.evaluate(() => (window as any).__coverage__)
    if (!cov) return
    mergeCoverage(masterCoverage, cov)
  } catch {
  }
}

export async function generateCoverageReport(): Promise<string> {
  if (Object.keys(masterCoverage).length === 0) return '0.0'

  if (!existsSync(REPORT_DIR)) mkdirSync(REPORT_DIR, { recursive: true })
  writeFileSync(join(COVERAGE_DIR, 'coverage-final.json'), JSON.stringify(masterCoverage, null, 2))

  const libCoverage = require('istanbul-lib-coverage')
  const libReport = require('istanbul-lib-report')
  const reports = require('istanbul-reports')

  const coverageMap = libCoverage.createCoverageMap(masterCoverage)
  const context = libReport.createContext({
    dir: REPORT_DIR,
    coverageMap,
    watermarks: { statements: [80, 90], branches: [70, 85], functions: [80, 90], lines: [80, 90] },
  })

  reports.create('lcovonly', {}).execute(context)
  const htmlReporter = reports.create('html', { subdir: 'html' })
  htmlReporter.execute(context)

  const summaries = coverageMap.getCoverageSummary()
  const linesPct = summaries.lines.pct

  return linesPct.toFixed(1)
}

function mergeCoverage(target: Record<string, unknown>, source: Record<string, unknown>): void {
  for (const [filePath, data] of Object.entries(source)) {
    if (target[filePath]) {
      target[filePath] = data
    } else {
      target[filePath] = data
    }
  }
}
