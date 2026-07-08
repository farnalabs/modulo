import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import type { Page } from '@playwright/test'

const FRONTEND_DIR = resolve(__dirname, '../../..')
const COVERAGE_DIR = resolve(__dirname, '../../../.coverage/e2e')

function viteUrlToPath(url: string): string {
  try {
    const pathname = new URL(url).pathname.replace(/^\//, '')
    return join(FRONTEND_DIR, pathname)
  } catch {
    return url
  }
}

export async function startCoverage(page: Page): Promise<void> {
  try {
    const cdp = await page.context().newCDPSession(page)
    await cdp.send('Profiler.enable')
    await cdp.send('Profiler.startPreciseCoverage', { callCount: true, detailed: true })
    ;(page as any).__cdpSession = cdp
  } catch {
  }
}

export async function stopCoverage(page: Page): Promise<void> {
  try {
    const cdp = (page as any).__cdpSession
    if (!cdp) return
    const result: { result: { scriptId: string; url: string; functions: any[] }[] } =
      await cdp.send('Profiler.takePreciseCoverage')
    await cdp.send('Profiler.stopPreciseCoverage')
    await cdp.detach()

    const coverageData: any[] = []
    for (const entry of result.result) {
      if (!entry.url.includes('/src/') || entry.url.includes('node_modules')) continue
      coverageData.push({
        path: viteUrlToPath(entry.url),
        functions: entry.functions.map((f: any) => ({
          functionName: f.functionName,
          ranges: (f.ranges || []).map((r: any) => ({ start: r.startOffset, end: r.endOffset, count: r.count ?? 0 })),
          isBlockCoverage: f.isBlockCoverage,
        })),
      })
    }

    const coverageFile = join(COVERAGE_DIR, `coverage-${Date.now()}-${Math.random().toString(36).slice(2)}.json`)
    if (!existsSync(COVERAGE_DIR)) mkdirSync(COVERAGE_DIR, { recursive: true })
    writeFileSync(coverageFile, JSON.stringify(coverageData))
  } catch {
  }
}
