import { test, expect, loginAsAdmin } from './setup/fixtures'
import type { Page } from '@playwright/test'

// Structural e2e for /onboarding (FAR-638, batch A). The wizard has 7 steps
// (Welcome → Connect Tools → Run Inference → Review Schemas → Browse Library →
// Wire Pipeline → Done) with Previous/Skip-to-end/Next footer navigation.
// Step data (connectors, library items) is environment-dependent, so only the
// step navigation render is pinned here; data-dependent bits are guarded to
// the local mock target.
test.describe('Onboarding Wizard', { tag: '@regression' }, () => {
  // The wizard card's step header (the sidebar brand also renders an h2, so
  // unscoped h2 locators would hit a strict-mode violation)
  const stepTitle = (page: Page) => page.locator('header.mb-6 h2')
  test.beforeEach(async ({ page }) => {
    // Force the Remy floating panel closed before the app boots so its
    // overlay can never cover page controls. Mirrors json-viewer.spec.ts.
    await page.addInitScript(() => {
      localStorage.setItem('remy-panel-state', 'closed')
    })
  })

  test('renders the welcome step', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/onboarding')

    await expect(page).toHaveURL(/\/onboarding/)
    await expect(page.locator('h1')).toContainText('SDLC Onboarding')
    await expect(stepTitle(page)).toContainText('Welcome')
    // Footer navigation: no Previous on step 0, Next enabled (step 0 always proceeds)
    await expect(page.getByTestId('onboarding-wizard-previous')).toHaveCount(0)
    await expect(page.getByTestId('onboarding-wizard-next')).toBeVisible()
    await expect(page.getByTestId('onboarding-wizard-next')).toBeEnabled()
  })

  test('navigates to the Connect Tools step', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/onboarding')
    await page.getByTestId('onboarding-wizard-next').click()

    await expect(stepTitle(page)).toContainText('Connect Tools')
    await expect(page.getByTestId('onboarding-wizard-previous')).toBeVisible()
    await expect(page.getByTestId('onboarding-wizard-skip-to-end')).toBeVisible()
    // Next is disabled until a connector is selected
    await expect(page.getByTestId('onboarding-wizard-next')).toBeDisabled()
    if (env.name === 'local') {
      // The local mock target has no connectors → empty state with a link out
      await expect(page.getByTestId('onboarding-wizard-create-connector')).toBeVisible()
    }
  })

  test('skip to end jumps to the Done step', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    await page.goto('/onboarding')
    await page.getByTestId('onboarding-wizard-next').click()
    await page.getByTestId('onboarding-wizard-skip-to-end').click()

    await expect(page.getByText("You're all set!")).toBeVisible()
    await expect(page.getByTestId('onboarding-wizard-go-to-dashboard')).toBeVisible()
    // Footer nav (including Previous) is intentionally hidden on the terminal Done step (v-if="currentStep < 6")
    await expect(page.getByTestId('onboarding-wizard-previous')).toHaveCount(0)
  })
})
