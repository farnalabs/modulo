import { test, expect, loginAsAdmin } from '../setup/fixtures'
import { apiBaseFor, apiFetch, apiLogin } from '../setup/realstack-api'

/**
 * Real-stack profile-preferences journey (FAR-1242 batch 2).
 *
 * Runs against the REAL backend (staging/app); skips the local target. The
 * caller's own HITL email-alert preference is a persisted account setting:
 * toggling it on the My Profile page and saving must reach the backend, and
 * the journey restores the original value so the shared admin account keeps
 * no drift.
 */

test.describe('Real-stack journeys: profile preferences', { tag: '@regression' }, () => {
  test.beforeEach(async ({ page, env }) => {
    test.skip(env.name === 'local', 'Real-stack journey — run with E2E_TARGET=staging (no API mocks here)')
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('toggling the HITL email-alert default on My Profile persists it (and is restored)', { tag: '@regression' }, async ({ page, env }) => {
    const apiBase = apiBaseFor(env)
    const token = await apiLogin(env)

    // Read the shared account's current value so the journey is a no-op net.
    const initialRes = await apiFetch<{ default: boolean }>(apiBase, token, 'GET', '/api/v1/me/hitl-email-preferences')
    expect(initialRes.status).toBe(200)
    const initialValue = initialRes.body?.default ?? false

    await loginAsAdmin(page, env)
    await page.goto('/admin/my-profile')

    const section = page.getByTestId('my-profile-hitl-email-section')
    await expect(section).toBeVisible({ timeout: 30_000 })
    const toggle = page.getByTestId('my-profile-hitl-email-default')

    // Toggle to the OPPOSITE of the stored value and save.
    const target = !initialValue
    if (target) {
      await toggle.setChecked(true)
    } else {
      await toggle.setChecked(false)
    }
    await page.getByTestId('my-profile-hitl-email-save').click()
    await expect(page.getByTestId('my-profile-hitl-email-success')).toBeVisible({ timeout: 30_000 })

    // Persisted: the backend now reports the flipped default.
    const afterRes = await apiFetch<{ default: boolean }>(apiBase, token, 'GET', '/api/v1/me/hitl-email-preferences')
    expect(afterRes.status).toBe(200)
    expect(afterRes.body?.default).toBe(target)

    // Restore the original value through the same seam and prove it stuck.
    if (initialValue) {
      await toggle.setChecked(true)
    } else {
      await toggle.setChecked(false)
    }
    await page.getByTestId('my-profile-hitl-email-save').click()
    await expect(page.getByTestId('my-profile-hitl-email-success')).toBeVisible({ timeout: 30_000 })
    const restoreRes = await apiFetch<{ default: boolean }>(apiBase, token, 'GET', '/api/v1/me/hitl-email-preferences')
    expect(restoreRes.status).toBe(200)
    expect(restoreRes.body?.default).toBe(initialValue)
  })
})
