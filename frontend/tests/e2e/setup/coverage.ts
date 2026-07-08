import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import type { Page } from '@playwright/test'

const COVERAGE_DIR = resolve(__dirname, '../../../.coverage/e2e')
const REPORT_DIR = join(COVERAGE_DIR, 'report')

interface CoverageEntry {
  url: string
  coveredBytes: number
  totalBytes: number
}

const allEntries: CoverageEntry[] = []

export async function startCoverage(page: Page): Promise<void> {
  await page.coverage.startJSCoverage({ resetOnNavigation: true })
}

export async function stopCoverage(page: Page): Promise<void> {
  try {
    const entries = await page.coverage.stopJSCoverage()
    for (const entry of entries) {
      if (!entry.url.includes('/src/') || entry.url.includes('node_modules')) continue
      let coveredBytes = 0
      for (const range of entry.ranges) {
        coveredBytes += range.end - range.start
      }
      allEntries.push({
        url: entry.url,
        coveredBytes,
        totalBytes: entry.source.length,
      })
    }
  } catch {
  }
}

export async function generateCoverageReport(): Promise<string> {
  if (allEntries.length === 0) return '0.0'

  if (!existsSync(REPORT_DIR)) mkdirSync(REPORT_DIR, { recursive: true })

  const totalBytes = allEntries.reduce((s, e) => s + e.totalBytes, 0)
  const coveredBytes = allEntries.reduce((s, e) => s + e.coveredBytes, 0)
  const pct = totalBytes > 0 ? ((coveredBytes / totalBytes) * 100).toFixed(1) : '0.0'

  const report = {
    summary: { coveredBytes, totalBytes, coveragePct: parseFloat(pct) },
    files: allEntries.sort((a, b) => a.coveredBytes / a.totalBytes - b.coveredBytes / b.totalBytes),
  }
  writeFileSync(join(REPORT_DIR, 'coverage-report.json'), JSON.stringify(report, null, 2))

  return pct
}
