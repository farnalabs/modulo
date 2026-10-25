import { test, expect } from './setup/fixtures'
import { installTeamTierNavigationGuard } from './setup/target-tier'
import type { TestEnv } from './setup/env'

/**
 * Unit-level proof of the team-tier navigation guard installed by the `page`
 * fixture (setup/target-tier.ts). Uses a fake Page so it needs neither a
 * browser nor a live target, and runs on every target.
 */

const staging = { name: 'staging' } as TestEnv
const app = { name: 'app' } as TestEnv

interface FakePage {
  route: (pattern: string | RegExp) => Promise<void>
  goto: (url: string) => Promise<null>
}

function fakePage(): { page: FakePage; calls: string[] } {
  const calls: string[] = []
  const page: FakePage = {
    route: async (pattern) => {
      calls.push(`route:${String(pattern)}`)
    },
    goto: async (url) => {
      calls.push(`goto:${url}`)
      return null
    },
  }
  return { page, calls }
}

async function run(
  env: TestEnv,
  path: string,
  tier: string,
  controlPlan = false,
): Promise<{ skips: string[]; calls: string[]; threw: boolean }> {
  const { page, calls } = fakePage()
  const skips: string[] = []
  // The real skip (`testInfo.skip`) throws to abort the test; model that so
  // the assertion also proves the navigation never happened.
  installTeamTierNavigationGuard(
    page as never,
    env,
    (reason) => {
      skips.push(reason)
      throw new Error('SKIP')
    },
    async () => tier,
  )
  let threw = false
  try {
    if (controlPlan) await page.route('**/api/v1/plan*')
    await page.goto(path)
  } catch (err) {
    if ((err as Error).message !== 'SKIP') throw err
    threw = true
  }
  return { skips, calls, threw }
}

test.describe('Team-tier navigation guard', { tag: '@regression' }, () => {
  test('community staging skips a team route without navigating', { tag: '@regression' }, async () => {
    const result = await run(staging, '/admin/connectors', 'community')
    expect(result.threw).toBe(true)
    expect(result.skips).toHaveLength(1)
    expect(result.skips[0]).toContain('/admin/connectors')
    expect(result.calls).not.toContain('goto:/admin/connectors')
  })

  test('community staging skips a legacy alias of a team route', { tag: '@regression' }, async () => {
    const result = await run(staging, '/admin/sandbox-concurrency', 'community')
    expect(result.threw).toBe(true)
    expect(result.calls).not.toContain('goto:/admin/sandbox-concurrency')
  })

  test('community staging still navigates a community route', { tag: '@regression' }, async () => {
    const result = await run(staging, '/admin/users', 'community')
    expect(result.threw).toBe(false)
    expect(result.skips).toHaveLength(0)
    expect(result.calls).toContain('goto:/admin/users')
  })

  test('team staging navigates team routes', { tag: '@regression' }, async () => {
    const result = await run(staging, '/admin/costs/limits', 'team')
    expect(result.threw).toBe(false)
    expect(result.calls).toContain('goto:/admin/costs/limits')
  })

  test('a spec-owned plan mock disables the guard', { tag: '@regression' }, async () => {
    const result = await run(staging, '/admin/notification-delivery', 'community', true)
    expect(result.threw).toBe(false)
    expect(result.calls).toContain('goto:/admin/notification-delivery')
  })

  test('the production target is never guarded', { tag: '@regression' }, async () => {
    const result = await run(app, '/admin/connectors', 'community')
    expect(result.threw).toBe(false)
    expect(result.calls).toContain('goto:/admin/connectors')
  })
})
