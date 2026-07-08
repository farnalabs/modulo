import type { FullConfig } from '@playwright/test'
import { generateCoverageReport } from './coverage'

async function coverageTeardown(config: FullConfig) {
  const pct = await generateCoverageReport()
  if (pct !== '0.0') {
    console.log(`\n  E2E code coverage: ${pct}%`)
  }
}

export default coverageTeardown
