import { test as base } from '@playwright/test'
import { startCoverage, stopCoverage } from './coverage'

export const test = base.extend({
  page: async ({ page }, use) => {
    const enabled = process.env.VITE_COVERAGE === 'true'
    if (enabled) await startCoverage(page)
    await use(page)
    if (enabled) await stopCoverage(page)
  },
})

export { expect } from '@playwright/test'
