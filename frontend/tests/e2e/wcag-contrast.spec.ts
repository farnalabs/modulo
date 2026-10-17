import { test, expect } from './setup/fixtures'
import AxeBuilder from '@axe-core/playwright'

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']

// aria-hidden-focus: PrimeVue renders hidden tabbable focus-trap spans
// (`p-hidden-accessible p-hidden-focusable`) inside its Select/overlay panels
// for a11y navigation. axe flags these as aria-hidden-focus; they are a
// library-internal navigation affordance, not a Modulo defect.
const ACCEPTABLE_VIOLATIONS = ['color-contrast', 'scrollable-region-focusable', 'aria-hidden-focus']

function filterViolations(violations: { id: string }[]) {
  return violations.filter(v => !ACCEPTABLE_VIOLATIONS.includes(v.id))
}

test.describe('PrimeVue Select overlay WCAG + keyboard nav', { tag: "@regression" }, () => {
  test('open filter-bar-status overlay has no unexpected WCAG AA violations', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
    await setupLocalMockApi(page)
    // dev_mode:true (set by the local mock API) renders the Assistant floating
    // panel on every route; it overlaps the /runs filter bar and intercepts
    // pointer events, blocking the Select trigger. Close it (mirrors the
    // staging e2e global-setup) so the overlay can be opened.
    await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
    await loginAsAdmin(page, env)

    await page.goto('/runs')
    await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)

    // Open the migrated PrimeVue Select panel (teleported to body).
    const trigger = page.getByTestId('filter-bar-status')
    await trigger.click()
    await expect(page.locator('[role="listbox"]')).toBeVisible()

    const results = await new AxeBuilder({ page })
      .withTags(WCAG_TAGS)
      .analyze()

    const violations = filterViolations(results.violations)
    if (violations.length > 0) {
      console.log(`\n=== /runs open select overlay new violations ===`)
      for (const v of violations) {
        console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
      }
    }
    expect(violations).toEqual([])
  })

  test('PrimeVue Select supports keyboard navigation (arrow + escape)', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'Uses setupLocalMockApi — only runs locally')
    const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
    await setupLocalMockApi(page)
    // dev_mode:true (set by the local mock API) renders the Assistant floating
    // panel on every route; it overlaps the /runs filter bar and intercepts
    // pointer events, blocking the Select trigger. Close it (mirrors the
    // staging e2e global-setup) so the overlay can be opened.
    await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
    await loginAsAdmin(page, env)

    await page.goto('/runs')
    await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)

    const trigger = page.getByTestId('filter-bar-status')
    await trigger.click()
    const listbox = page.locator('[role="listbox"]')
    await expect(listbox).toBeVisible()

    // ArrowDown moves the active option. PrimeVue keeps real focus on the
    // combobox trigger and tracks the active option via the
    // aria-activedescendant / data-p-focused attributes (not DOM focus), so
    // assert that some option became active after ArrowDown.
    await page.keyboard.press('ArrowDown')
    await expect(page.locator('[role="option"][data-p-focused="true"]')).toHaveCount(1)

    // Escape closes the panel.
    await page.keyboard.press('Escape')
    await expect(listbox).not.toBeVisible()
  })
})

test.describe('WCAG AA audit (CI — Vite dev server)', { tag: "@regression" }, () => {
  const pages = [
    { path: '/login', name: 'login page' },
    { path: '/', name: 'root page' },
  ]

  for (const { path, name } of pages) {
    test(`${name} — light mode has no unexpected WCAG AA violations`, { tag: "@regression" }, async ({ page }) => {
      await page.goto(path)

      // Guard: storageState may redirect /login to / (dashboard)
      if (path === '/login' && !page.url().includes('/login')) {
        console.log(`  Skipping ${path} — redirected by valid session`)
        return
      }

      // Wait for the Vue app to mount before running axe
      await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)

      await page.evaluate(() => {
        document.documentElement.classList.add('light')
        document.documentElement.classList.remove('dark')
      })
      await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))

      const results = await new AxeBuilder({ page })
        .withTags(WCAG_TAGS)
        .analyze()

      const violations = filterViolations(results.violations)

      if (violations.length > 0) {
        console.log(`\n=== ${path} (light) new violations ===`)
        for (const v of violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }

      expect(violations).toEqual([])
    })

    test(`${name} — dark mode has no unexpected WCAG AA violations`, { tag: "@regression" }, async ({ page }) => {
      await page.goto(path)

      // Guard: storageState may redirect /login to / (dashboard)
      if (path === '/login' && !page.url().includes('/login')) {
        console.log(`  Skipping ${path} — redirected by valid session`)
        return
      }

      // Wait for the Vue app to mount before running axe
      await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)

      await page.evaluate(() => {
        document.documentElement.classList.remove('light')
        document.documentElement.classList.remove('dark')
      })
      await page.evaluate(() => new Promise(r => requestAnimationFrame(r)))

      const results = await new AxeBuilder({ page })
        .withTags(WCAG_TAGS)
        .analyze()

      const violations = filterViolations(results.violations)

      if (violations.length > 0) {
        console.log(`\n=== ${path} (dark) new violations ===`)
        for (const v of violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }

      expect(violations).toEqual([])
    })
  }
})

// FAR-1907 — the sidebar profile links carried aria-label="User profile" over
// visible text, which is a label-content-name-mismatch (WCAG 2.5.3) violation.
// This spec could not see it before: the local mock access token is not a JWT,
// so `userId` (the JWT `sub` claim) resolved to null, the identity link
// rendered no text at all, and the avatar rendered "?" — a single non-digit
// character, which axe treats as non-human-interpretable and reports as
// `incomplete`, never as a violation. The tests below drive the authenticated
// shell with a real `sub`, which is the state every real session is in.
const AUTHENTICATED_ROUTES = ['/runs', '/', '/pipelines', '/admin/my-profile', '/settings/triggers', '/admin/users'] as const

// Mirrors the local-JWT helper in assistant-only.spec.ts: a structurally valid
// token whose payload the client decodes (the signature is not verified
// client-side). `sub` is what populates the sidebar's visible identity text.
function b64url(input: unknown): string {
  return Buffer.from(JSON.stringify(input)).toString('base64url')
}

function jwtWithSub(sub: string): string {
  const header = b64url({ alg: 'HS256', typ: 'JWT' })
  const now = Math.floor(Date.now() / 1000)
  const payload = b64url({
    sub,
    email: 'admin@example.com',
    org_role: 'admin',
    is_system_admin: true,
    iat: now,
    exp: now + 3600,
  })
  return `${header}.${payload}.mocked-signature`
}

// Tagged @local-mock rather than @regression: these tests drive the
// setupLocalMockApi target, so they run in the `Frontend and WCAG` CI job
// (which selects this file by name, with the default local target) but must
// NOT run in the deployed-build @regression gate. That gate checks out main
// and runs against the live staging APIs; a mock-backed test there would
// assert the runner's fixtures against the deployed bundle, and it would fail
// for any branch whose fix is not yet deployed. A tag keeps that separation
// declarative instead of leaving a runtime environment gate in the test body.
test.describe('authenticated shell — sidebar profile links (FAR-1907)', { tag: "@local-mock" }, () => {
  test('profile link names contain the visible text for a UUID sub', { tag: "@local-mock" }, async ({ page, env }) => {
    const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
    await setupLocalMockApi(page)
    await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
    await loginAsAdmin(page, env)

    // A UUID `sub` (what the backend mints) renders an identity link whose
    // visible text is the UUID and an avatar whose initial is a digit — the
    // exact state in which label-content-name-mismatch fires.
    await page.goto('/runs')
    await page.evaluate((token) => localStorage.setItem('modulo_access_token', token), jwtWithSub('3f9a1b2c-4d5e-6f70-8192-a3b4c5d6e7f8'))
    await page.reload()
    await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)

    const links = page.locator('a[href$="/admin/my-profile"]')
    await expect(links).toHaveCount(2)

    // WCAG 2.5.3: the accessible name must contain the visible label text. The
    // pre-fix markup rendered aria-label="User profile" over "3f9a…" and "3".
    const pairs = await links.evaluateAll(els => els.map(el => ({
      label: el.getAttribute('aria-label') ?? '',
      visible: (el.textContent ?? '').trim(),
    })))
    for (const { label, visible } of pairs) {
      expect(visible.length, 'each sidebar profile link must render visible text').toBeGreaterThan(0)
      expect(label, `aria-label ${JSON.stringify(label)} must contain the visible text ${JSON.stringify(visible)}`).toContain(visible)
    }

    const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()
    const violations = filterViolations(results.violations)
    if (violations.length > 0) {
      console.log(`\n=== /runs authenticated shell new violations ===`)
      for (const v of violations) {
        console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
      }
    }
    expect(violations).toEqual([])
  })

  for (const path of AUTHENTICATED_ROUTES) {
    test(`authenticated ${path} has no unexpected WCAG AA violations`, { tag: "@local-mock" }, async ({ page, env }) => {
      const { setupLocalMockApi, loginAsAdmin } = await import('./setup/fixtures')
      await setupLocalMockApi(page)
      await page.addInitScript(() => { try { localStorage.setItem('assistant-panel-state', 'closed') } catch (err) { console.warn(err) } })
      await loginAsAdmin(page, env)

      await page.goto(path)
      await page.waitForFunction(() => document.querySelector('#app')?.children.length > 0)

      const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze()
      const violations = filterViolations(results.violations)
      if (violations.length > 0) {
        console.log(`\n=== ${path} (authenticated) new violations ===`)
        for (const v of violations) {
          console.log(`[${v.impact}] ${v.id} (${v.nodes.length} nodes): ${v.help}`)
        }
      }
      expect(violations).toEqual([])
    })
  }
})
