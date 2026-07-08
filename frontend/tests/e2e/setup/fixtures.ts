import { test as base } from '@playwright/test'
import { startCoverage, stopCoverage } from './coverage'

export const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    const coverageEnabled = process.env.VITE_COVERAGE === 'true'
    if (coverageEnabled) {
      await startCoverage(page)
    }
    await use(page)
    if (coverageEnabled) {
      await stopCoverage(page, testInfo)
    }
  },
})

export { expect } from '@playwright/test'
