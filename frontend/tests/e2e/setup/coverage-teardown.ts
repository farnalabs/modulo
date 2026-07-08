import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const COVERAGE_DIR = resolve(__dirname, '../../../.coverage/e2e')
const REPORT_DIR = join(COVERAGE_DIR, 'report')

async function coverageTeardown(config: any) {
  if (!existsSync(COVERAGE_DIR)) {
    console.log('\n  E2E code coverage: no coverage directory found')
    return
  }

  const files = readdirSync(COVERAGE_DIR).filter(f => f.startsWith('coverage-') && f.endsWith('.json'))
  if (files.length === 0) {
    console.log('\n  E2E code coverage: no coverage files found')
    return
  }

  console.log(`\n  E2E code coverage: processing ${files.length} coverage files`)

  const { default: V8ToIstanbul } = await import('v8-to-istanbul')
  const libCoverage = require('istanbul-lib-coverage')
  const libReport = require('istanbul-lib-report')
  const reports = require('istanbul-reports')

  const coverageMap: Record<string, any> = {}

  for (const file of files) {
    const data: { path: string; functions: any[] }[] = JSON.parse(
      readFileSync(join(COVERAGE_DIR, file), 'utf-8'),
    )
    for (const entry of data) {
      try {
        const script = V8ToIstanbul(entry.path)
        await script.load()
        script.applyCoverage(entry.functions)
        Object.assign(coverageMap, script.toIstanbul())
      } catch {
      }
    }
  }

  if (Object.keys(coverageMap).length === 0) {
    console.log('  E2E code coverage: no coverage could be mapped to source files')
    return
  }

  if (!existsSync(REPORT_DIR)) mkdirSync(REPORT_DIR, { recursive: true })
  writeFileSync(join(COVERAGE_DIR, 'coverage-final.json'), JSON.stringify(coverageMap, null, 2))

  const map = libCoverage.createCoverageMap(coverageMap)
  const context = libReport.createContext({
    dir: REPORT_DIR,
    coverageMap: map,
    watermarks: { statements: [80, 90], branches: [70, 85], functions: [80, 90], lines: [80, 90] },
  })
  reports.create('lcovonly', {}).execute(context)
  reports.create('html', { subdir: 'html' }).execute(context)

  const summary = map.getCoverageSummary()
  console.log(`  Lines:      ${summary.lines.pct}%`)
  console.log(`  Statements: ${summary.statements.pct}%`)
  console.log(`  Branches:   ${summary.branches.pct}%`)
  console.log(`  Functions:  ${summary.functions.pct}%`)
}

export default coverageTeardown
