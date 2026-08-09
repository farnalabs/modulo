import { test, expect, loginAsAdmin } from './setup/fixtures'

test.describe('JsonViewer design-token theming', { tag: '@regression' }, () => {
  // Local runs need a generous timeout: the first SPA bundle compile on the
  // dev server can exceed the 30s default before any assertion runs.
  test.setTimeout(90_000)

  test('renders vue-json-pretty with token colours and no dep palette leaks', async ({ page, env }) => {
    await loginAsAdmin(page, env)

    const runPayload = {
      run_id: 'run-json-viewer',
      run_number: 42,
      pipeline_id: 'p-1',
      pipeline_name: 'JSON Viewer Test',
      status: 'complete',
      trace_id: 'trace-abc',
      total_cost_usd: '0.000100',
      node_token_usage: {
        format: { input_tokens: 10, output_tokens: 20, total_tokens: 30, model_cost_display_usd: 0.0001 },
      },
    }

    await page.route('**/api/v1/runs/run-json-viewer', (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(runPayload) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    await page.route('**/api/v1/runs/run-json-viewer/io', (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          outputs_json: {
            format: {
              input: { prompt: 'hello world', count: 3, flag: true, nothing: null, items: ['x', 'y'] },
              output: { result: 'success', score: 0.98 },
            },
          },
        }),
      })
    })
    await page.route('**/api/v1/runs/run-json-viewer/workspace-lease', (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'completed' }) })
    })
    await page.route('**/api/v1/runs/run-json-viewer/events*', (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) })
    })

    await page.goto('/runs/run-json-viewer')
    await expect(page.locator('.json-viewer').first()).toBeVisible()

    await page.getByTestId('run-detail-toggle-io').click()
    await expect(page.locator('.vjs-value-string').first()).toBeVisible()

    const probe = (selector: string, token: string) =>
      page.evaluate(
        ({ sel, tok }) => {
          const p = document.createElement('span')
          p.style.color = `hsl(var(--${tok}))`
          document.body.appendChild(p)
          const expected = getComputedStyle(p).color
          const el = document.querySelector(sel)
          const actual = el ? getComputedStyle(el).color : null
          p.remove()
          return { expected, actual }
        },
        { sel: selector, tok: token },
      )

    const probes = [
      ['.vjs-value-string', 'success'],
      ['.vjs-key', 'muted-foreground'],
      ['.vjs-value-number', 'primary'],
      ['.vjs-value-null', 'warning'],
      ['.vjs-tree-brackets', 'foreground'],
    ] as const

    for (const [selector, token] of probes) {
      const { expected, actual } = await probe(selector, token)
      expect(actual, `expected ${selector} to resolve ${token} token`).toBe(expected)
    }

    const canary = await page.evaluate(() => {
      const depColours = [
        'rgb(24, 144, 255)', // #1890ff
        'rgb(19, 206, 102)', // #13ce66
        'rgb(29, 140, 224)', // #1d8ce0
        'rgb(213, 95, 222)', // #d55fde
        'rgb(191, 203, 217)', // #bfcbd9
        'rgb(230, 247, 255)', // #e6f7ff
        'rgb(46, 69, 88)', // #2e4558
      ]
      const offenders: string[] = []
      const roots = Array.from(document.querySelectorAll('.json-viewer .vjs-tree'))
      if (roots.length === 0) return { clean: false, offenders: ['no .vjs-tree element'] }
      for (const root of roots) {
        const els = root.querySelectorAll('*')
        for (const el of Array.from(els)) {
          const color = getComputedStyle(el).color
          const bg = getComputedStyle(el).backgroundColor
          if (depColours.includes(color) || depColours.includes(bg)) {
            offenders.push(`${el.className} color=${color} bg=${bg}`)
          }
        }
      }
      return { clean: offenders.length === 0, offenders }
    })
    expect(canary.clean, `dep palette leaked: ${canary.offenders.join('; ')}`).toBe(true)

    await page.evaluate(() => document.documentElement.classList.add('light'))
    const lightProbe = await probe('.vjs-value-string', 'success')
    expect(lightProbe.actual, 'light-mode string value should follow the light success token').toBe(lightProbe.expected)
  })

  test('copy button writes the formatted JSON to the clipboard', async ({ page, env }) => {
    await loginAsAdmin(page, env)

    // The only JsonViewer rolled out with a toolbar is the failed-run input
    // payload card (`show-toolbar=true`) — use a failed run to render it.
    await page.route('**/api/v1/runs/run-json-viewer', (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          run_id: 'run-json-viewer',
          run_number: 42,
          pipeline_id: 'p-1',
          pipeline_name: 'JSON Viewer Test',
          status: 'failed',
          trace_id: 'trace-abc',
          total_cost_usd: '0.000100',
          node_token_usage: { format: { input_tokens: 10, output_tokens: 20, total_tokens: 30 } },
        }),
      })
    })
    await page.route('**/api/v1/runs/run-json-viewer/workspace-lease', (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'completed' }) })
    })
    await page.route('**/api/v1/runs/run-json-viewer/events*', (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) })
    })
    // The failed-run input payload card renders `input_payload`; supply it
    // alongside the node outputs in the io response.
    await page.route('**/api/v1/runs/run-json-viewer/io', (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          input_payload: { prompt: 'hello world', count: 3 },
          outputs_json: { format: { input: { prompt: 'hello world' }, output: { result: 'success' } } },
        }),
      })
    })

    await page.goto('/runs/run-json-viewer')
    await expect(page.getByTestId('json-viewer-copy')).toBeVisible()

    await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
    await page.getByTestId('json-viewer-copy').click()
    const clipboard = await page.evaluate(() => navigator.clipboard.readText())
    expect(clipboard).toContain('hello world')
  })
})
