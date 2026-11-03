import { test, expect, loginAsAdmin } from './setup/fixtures'

/**
 * Left edge of every rendered line of `el`, grouped by line (y). Chromium
 * reports the trailing space of a wrapped line as its own rect, so each
 * line's min x is the authoritative left edge.
 */
function collectLineLeftEdges(el: Element): number[] {
  const range = document.createRange()
  range.selectNodeContents(el)
  const byLine = new Map<number, number[]>()
  for (const r of Array.from(range.getClientRects()).filter((r) => r.width > 0)) {
    const y = Math.round(r.y)
    byLine.set(y, [...(byLine.get(y) ?? []), Math.round(r.x)])
  }
  return Array.from(byLine.values()).map((xs) => Math.min(...xs))
}

test.describe('JsonViewer design-token theming', { tag: '@regression' }, () => {
  // Local runs need a generous timeout: the first SPA bundle compile on the
  // dev server can exceed the 30s default before any assertion runs.
  test.setTimeout(90_000)

  test.beforeEach(async ({ page }) => {
    // The Assistant floating panel (rendered on staging where dev-mode is on) opens
    // by default and its fixed-position overlay intercepts clicks on interactive
    // page content, timing out locator.click() with "subtree intercepts pointer
    // events". Force the panel closed before the app boots so it can never cover
    // the JsonViewer toolbar/expand controls. Mirrors error-tracking.spec.ts.
    await page.addInitScript(() => {
      localStorage.setItem('assistant-panel-state', 'closed')
    })
  })

  test('renders vue-json-pretty with token colours and no dep palette leaks', { tag: '@regression' }, async ({ page, env }) => {
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

    // FAR-2016: value colours resolve through the TEXT-SAFE tokens
    // (--success-text / --primary-text), not the fill hues. In the dark and
    // agent themes each pair is byte-identical, so the difference only bites in
    // light mode — where --success is 2.30:1 on white and --success-text is
    // 5.42:1 (see the FAR-1938 comment in style.css).
    const probes = [
      ['.vjs-value-string', 'success-text'],
      ['.vjs-key', 'muted-foreground'],
      ['.vjs-value-number', 'primary-text'],
      ['.vjs-value-null', 'warning-text'],
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

    const lightProbe = await probe('.vjs-value-string', 'success-text')
    const lightFillHue = await probe('.vjs-value-string', 'success')
    expect(
      lightFillHue.expected,
      'light mode must keep --success-text distinct from the fill hue --success',
    ).not.toBe(lightProbe.expected)
    expect(lightProbe.actual, 'light-mode string value should follow the light success-text token').toBe(lightProbe.expected)
    expect(
      lightProbe.actual,
      'light-mode string value must not use the non-text-safe --success fill hue',
    ).not.toBe(lightFillHue.expected)
  })

  test('copy button writes the formatted JSON to the clipboard', { tag: '@regression' }, async ({ page, env }) => {
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

  test('truncates long string values and expands to the full value', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    const longText = 'The quick brown fox jumps over the lazy dog. '.repeat(60)
    const expectedCount = longText.length.toLocaleString('en-US')

    await page.route('**/api/v1/runs/run-json-viewer', (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            run_id: 'run-json-viewer',
            run_number: 42,
            pipeline_id: 'p-1',
            pipeline_name: 'JSON Viewer Test',
            status: 'complete',
            trace_id: 'trace-abc',
            total_cost_usd: '0.000100',
            node_token_usage: { format: { input_tokens: 10, output_tokens: 20, total_tokens: 30 } },
          }),
        })
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
              output: { result: 'success', agent_stdout: longText },
            },
          },
        }),
      })
    })
    await page.route('**/api/v1/runs/run-json-viewer/events*', (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) })
    })

    await page.goto('/runs/run-json-viewer')
    await expect(page.locator('.json-viewer').first()).toBeVisible()
    await page.getByTestId('run-detail-toggle-io').click()

    const truncated = page.getByTestId('json-viewer-string-truncated').first()
    await expect(truncated).toBeVisible()
    await expect(truncated).toContainText('Expand')
    await expect(truncated).toContainText(`… ${expectedCount} chars`)
    await expect(truncated).not.toContainText(longText.slice(500))

    await page.getByTestId('json-viewer-string-expand').first().click()
    await expect(page.getByTestId('json-viewer-string-expanded').first()).toContainText(longText)
  })

  test('wrapped long values stay inside their node without crushing the expand controls', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    // Two wrap shapes: a spaced sentence above the truncation threshold (the
    // truncated preview wraps to several lines at desktop width) and an
    // unbreakable token below it (the dep's default value path must wrap at
    // the container edge without horizontal overflow). FAR-626.
    const wrappedText = 'The quick brown fox jumps over the lazy dog. '.repeat(14)
    const unbreakableToken = 'x'.repeat(430)

    await page.route('**/api/v1/runs/run-json-viewer', (route) => {
      if (route.request().method() === 'GET') {
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
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    await page.route('**/api/v1/runs/run-json-viewer/events*', (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) })
    })
    await page.route('**/api/v1/runs/run-json-viewer/io', (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          input_payload: { big_text: wrappedText, session_blob: unbreakableToken },
          outputs_json: { format: { output: { result: 'success' } } },
        }),
      })
    })

    await page.goto('/runs/run-json-viewer')
    await expect(page.locator('[data-testid="run-detail-input-payload"]')).toBeVisible()
    await expect(page.locator('.json-viewer').first()).toBeVisible()

    const truncated = page.getByTestId('json-viewer-string-truncated').first()
    await expect(truncated).toBeVisible()

    // The expand toggle and char count keep their natural size next to the
    // wrapped preview — they must never be squeezed into vertical slivers.
    const toggleBox = await page.getByTestId('json-viewer-string-expand').first().boundingBox()
    expect(toggleBox, 'expand toggle rendered').not.toBeNull()
    expect(toggleBox!.width, 'expand toggle must keep its label width').toBeGreaterThan(40)
    expect(toggleBox!.height, 'expand toggle must stay one label tall').toBeLessThanOrEqual(30)
    const countBox = await truncated.locator('.json-viewer-string-count').boundingBox()
    expect(countBox, 'char count rendered').not.toBeNull()
    expect(countBox!.height, 'char count must stay one line tall').toBeLessThanOrEqual(20)

    // The wrapped preview's line boxes share one left edge (hanging indent at
    // the value start) and actually wrap.
    const lines = await truncated.locator('.json-viewer-string-truncated-text').evaluate(collectLineLeftEdges)
    expect(lines.length, 'preview wraps to several lines at desktop width').toBeGreaterThan(1)
    expect(new Set(lines).size, `wrapped lines share one left edge: ${JSON.stringify(lines)}`).toBe(1)

    // The default dep path wraps the unbreakable token at the container edge.
    const blobValue = page.locator('.vjs-value-string', { hasText: 'xxxxxxxxxx' }).first()
    await expect(blobValue).toBeVisible()
    const blobLines = await blobValue.evaluate(collectLineLeftEdges)
    expect(blobLines.length, 'unbreakable token wraps to several lines').toBeGreaterThan(1)
    expect(new Set(blobLines).size, `wrapped token lines share one left edge: ${JSON.stringify(blobLines)}`).toBe(1)

    // Nothing spills out of the scroll container horizontally.
    const overflow = await page.evaluate(() => {
      const el = document.querySelector('.json-viewer .overflow-auto')
      return el ? el.scrollWidth - el.clientWidth : null
    })
    expect(overflow, 'no horizontal overflow in the viewer scroll container').toBe(0)
  })

  test.describe('narrow viewport — FAR-1840', () => {
    // iPhone-ish viewport: the reported bug is that a long string value
    // collapses to one character per line because the shrink-proof expand
    // toggle + char count squeeze the preview text.
    test.use({ viewport: { width: 375, height: 812 }, deviceScaleFactor: 1, isMobile: true, hasTouch: true })

    test('long string value fills the row and the expand control stays reachable', { tag: '@regression' }, async ({ page, env }) => {
      await loginAsAdmin(page, env)

      const longText = 'The quick brown fox jumps over the lazy dog. '.repeat(60)

      await page.route('**/api/v1/runs/run-json-viewer', (route) => {
        if (route.request().method() === 'GET') {
          return route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
              run_id: 'run-json-viewer',
              run_number: 42,
              pipeline_id: 'p-1',
              pipeline_name: 'JSON Viewer Test',
              status: 'complete',
              trace_id: 'trace-abc',
              total_cost_usd: '0.000100',
              node_token_usage: { format: { input_tokens: 10, output_tokens: 20, total_tokens: 30 } },
            }),
          })
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
                // First key (not last) so the row also carries the dep's
                // trailing comma — the comma must not break the layout.
                agent_stdout: longText,
                result: 'success',
              },
            },
          }),
        })
      })
      await page.route('**/api/v1/runs/run-json-viewer/events*', (route) => {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) })
      })

      await page.goto('/runs/run-json-viewer')
      await expect(page.locator('.json-viewer').first()).toBeVisible()
      await page.getByTestId('run-detail-toggle-io').click()

      const truncated = page.getByTestId('json-viewer-string-truncated').first()
      await expect(truncated).toBeVisible()

      // Measure the preview's actual line boxes. A single-character column
      // shows up as ~one line per character (hundreds of near-zero-width
      // line rects); the fixed layout wraps at the container width.
      const metrics = await truncated.evaluate((el) => {
        const text = el.querySelector('.json-viewer-string-truncated-text') as HTMLElement | null
        if (!text) return null
        const range = document.createRange()
        range.selectNodeContents(text)
        const byLine = new Map<number, number>()
        for (const r of Array.from(range.getClientRects())) {
          if (r.width <= 0 || r.height <= 0) continue
          const y = Math.round(r.y)
          byLine.set(y, Math.max(byLine.get(y) ?? 0, r.width))
        }
        const lineWidths = Array.from(byLine.values())
        const textBox = text.getBoundingClientRect()
        const scroll = el.closest('.overflow-auto')
        const toggle = el.querySelector('[data-testid="json-viewer-string-expand"]')
        const toggleBox = toggle?.getBoundingClientRect()
        return {
          lineCount: lineWidths.length,
          widestLine: Math.max(...lineWidths),
          narrowestLine: Math.min(...lineWidths),
          previewChars: (text.textContent ?? '').length,
          textBoxWidth: Math.round(textBox.width),
          containerWidth: scroll ? Math.round(scroll.getBoundingClientRect().width) : 0,
          horizontalOverflow: scroll ? scroll.scrollWidth - scroll.clientWidth : null,
          toggleWidth: toggleBox ? Math.round(toggleBox.width) : 0,
          toggleHeight: toggleBox ? Math.round(toggleBox.height) : 0,
        }
      })
      expect(metrics, 'truncated preview rendered').not.toBeNull()
      console.log(`[far-1840] narrow-viewport metrics: ${JSON.stringify(metrics!)}`)

      // 1. The value fills the row: the preview takes most of the viewer's
      //    width instead of being squeezed next to the controls.
      expect(metrics!.textBoxWidth, `value must fill the row (got ${metrics!.textBoxWidth}px of ${metrics!.containerWidth}px container)`).toBeGreaterThan(
        metrics!.containerWidth * 0.5,
      )

      // 2. NOT a single-character column: every wrapped line carries real
      //    text, and the 500-char preview wraps to a sane number of lines
      //    (one char per line would be ~500 line boxes).
      expect(metrics!.widestLine, `widest preview line must carry more than one character (got ${metrics!.widestLine}px)`).toBeGreaterThan(40)
      expect(metrics!.lineCount, `preview must not wrap one character per line (got ${metrics!.lineCount} lines for ${metrics!.previewChars} chars)`).toBeLessThan(
        metrics!.previewChars / 5,
      )

      // 3. The controls keep their natural size and stay reachable.
      expect(metrics!.toggleWidth, 'expand toggle must keep its label width').toBeGreaterThan(40)
      expect(metrics!.toggleHeight, 'expand toggle must stay one label tall').toBeLessThanOrEqual(30)

      // 4. The row does not force horizontal scrolling on the viewport.
      expect(metrics!.horizontalOverflow, 'no horizontal overflow in the viewer scroll container').toBe(0)

      // 5. The value sits in the KEY's column (FAR-1861, FAR-1840 follow-up):
      //    every wrapped line starts at the key's left edge, not the row's
      //    edge (pre-fix measurement: value x58 vs key x73), and the indent
      //    column spans the whole node so the guides keep covering it.
      const geometry = await truncated.evaluate((el) => {
        const node = el.closest('.vjs-tree-node')
        const key = node?.querySelector('.vjs-key')
        const indent = node?.querySelector('.vjs-indent')
        if (!node || !key || !indent) return null
        const keyBox = key.getBoundingClientRect()
        const nodeBox = node.getBoundingClientRect()
        const indentBox = indent.getBoundingClientRect()
        return {
          nodeLeft: Math.round(nodeBox.x),
          keyLeft: Math.round(keyBox.x),
          indentHeight: Math.round(indentBox.height),
          nodeHeight: Math.round(nodeBox.height),
        }
      })
      expect(geometry, 'key/indent geometry rendered').not.toBeNull()
      // Per-line left edges (the helper groups Chromium's per-line rects —
      // including the trailing-space rect each wrapped line emits — by y and
      // takes the min x as that line's left edge).
      const valueLefts = await truncated
        .locator('.json-viewer-string-truncated-text')
        .evaluate(collectLineLeftEdges)
      // The key must actually be indented past the row edge — otherwise
      // "value aligned to key" would be vacuously true at x of the row.
      expect(geometry!.keyLeft, 'key sits right of the row edge (indent > 0)').toBeGreaterThan(geometry!.nodeLeft + 2)
      // Every wrapped line of the value shares the key's left edge...
      expect(valueLefts.length, 'preview wraps to several lines').toBeGreaterThan(1)
      for (const left of valueLefts) {
        expect(
          Math.abs(left - geometry!.keyLeft),
          `value line at x${left} must align to key at x${geometry!.keyLeft} (lines: ${JSON.stringify(valueLefts)})`,
        ).toBeLessThanOrEqual(1)
      }
      // ...and the indent column spans the node, so the guide lines cover
      // the wrapped value's rows instead of stopping at the first line.
      expect(
        Math.abs(geometry!.indentHeight - geometry!.nodeHeight),
        `indent ${geometry!.indentHeight}px must span node ${geometry!.nodeHeight}px`,
      ).toBeLessThanOrEqual(2)

      // 6. The expand control actually works at this viewport.
      await page.getByTestId('json-viewer-string-expand').first().click()
      const expanded = page.getByTestId('json-viewer-string-expanded').first()
      await expect(expanded).toContainText(longText.slice(0, 60))

      await page.getByTestId('json-viewer-string-collapse').first().click()
      await expect(truncated).toBeVisible()
      await expect(page.getByTestId('json-viewer-string-expanded')).toHaveCount(0)
    })
  })

  test('keeps the trailing comma with its truncated value (FAR-1901)', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    // agent_stdout is NOT the last key, so vue-json-pretty renders the value's
    // trailing comma as its own classless <span> after the (truncated) value.
    const longText = 'The quick brown fox jumps over the lazy dog. '.repeat(60)

    await page.route('**/api/v1/runs/run-json-viewer', (route) => {
      if (route.request().method() === 'GET') {
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
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    })
    await page.route('**/api/v1/runs/run-json-viewer/events*', (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) })
    })
    await page.route('**/api/v1/runs/run-json-viewer/io', (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ input_payload: { agent_stdout: longText, result: 'success' } }),
      })
    })

    await page.goto('/runs/run-json-viewer')
    await expect(page.locator('[data-testid="run-detail-input-payload"]')).toBeVisible()

    const truncated = page.getByTestId('json-viewer-string-truncated').first()
    await expect(truncated).toBeVisible()
    await truncated.scrollIntoViewIfNeeded()

    const geometry = await truncated.evaluate((el) => {
      // The value wrapper is the dep's classless <span> (the truncated span's
      // grandparent); the trailing comma is the wrapper's own classless <span>
      // sibling of `.vjs-value`.
      const wrapper = el.parentElement?.parentElement
      const comma = wrapper?.querySelector(':scope > span:not([class])') as HTMLElement | null
      const text = el.querySelector('.json-viewer-string-truncated-text') as HTMLElement | null
      if (!comma || !text) return null

      // Per-line boxes of the whole truncated value (preview text + its
      // controls), keyed by line-box top. The comma is a sibling of the
      // value, so it must share the value's last line box.
      const range = document.createRange()
      range.selectNodeContents(el)
      const byLine = new Map<number, { top: number; bottom: number }>()
      for (const r of Array.from(range.getClientRects()).filter((r) => r.width > 0)) {
        const top = Math.round(r.top)
        const prior = byLine.get(top)
        byLine.set(top, {
          top,
          bottom: prior ? Math.max(prior.bottom, Math.round(r.bottom)) : Math.round(r.bottom),
        })
      }
      const lines = Array.from(byLine.values()).sort((a, b) => a.top - b.top)
      const lastLine = lines[lines.length - 1]
      const commaRect = comma.getBoundingClientRect()
      const blockRect = el.getBoundingClientRect()
      // The scroller is the viewer's own `overflow-auto` container; the comma
      // must sit inside it (not clipped by horizontal scrolling).
      const scroller = el.closest('.overflow-auto')?.getBoundingClientRect()
      const commaStyle = getComputedStyle(comma)

      return {
        lineCount: lines.length,
        lastLineTop: lastLine?.top ?? null,
        commaTop: Math.round(commaRect.top),
        commaLeft: Math.round(commaRect.left),
        commaWidth: Math.round(commaRect.width),
        commaHeight: Math.round(commaRect.height),
        commaText: comma.textContent,
        commaDisplay: commaStyle.display,
        blockTop: Math.round(blockRect.top),
        blockBottom: Math.round(blockRect.bottom),
        blockLeft: Math.round(blockRect.left),
        withinScroller:
          scroller !== undefined &&
          commaRect.left >= scroller.left - 1 &&
          commaRect.right <= scroller.right + 1 &&
          commaRect.top >= scroller.top - 1 &&
          commaRect.bottom <= scroller.bottom + 1,
      }
    })
    expect(geometry, 'truncated value + trailing comma rendered').not.toBeNull()
    console.log(`[far-1901] desktop trailing-comma geometry: ${JSON.stringify(geometry)}`)

    // The defect needs a multi-line value: the preview wraps at desktop width.
    expect(geometry!.lineCount, 'preview wraps to several lines at desktop width').toBeGreaterThan(1)

    // The trailing comma shares the value's FINAL line — its top sits inside the
    // last line box. Pre-fix the comma was pushed onto its own line ~20px below
    // the last line (a full line-height lower), so this is the discriminating
    // assertion.
    expect(
      Math.abs(geometry!.commaTop - geometry!.lastLineTop!),
      `comma top ${geometry!.commaTop} must sit on the value's last line (top ${geometry!.lastLineTop})`,
    ).toBeLessThanOrEqual(4)

    // ...and it stays within the value block rather than on a line beneath it.
    expect(
      geometry!.commaTop,
      `comma top ${geometry!.commaTop} must stay inside the value block (${geometry!.blockTop}..${geometry!.blockBottom})`,
    ).toBeLessThan(geometry!.blockBottom)

    // The punctuator is the comma glyph, rendered and inside the viewer's
    // scroll box (a regression that removed or clipped it fails here).
    expect(geometry!.commaText, 'trailing punctuator is the comma glyph').toBe(',')
    expect(geometry!.commaDisplay, 'comma is displayed').not.toBe('none')
    expect(geometry!.commaWidth, 'comma has a non-zero width').toBeGreaterThan(0)
    expect(geometry!.commaHeight, 'comma has a non-zero height').toBeGreaterThan(0)
    expect(geometry!.withinScroller, 'comma sits inside the viewer scroll container').toBe(true)

    // It follows the value content on that line rather than being wrapped back
    // to the row's left edge (the pre-fix comma sat at the value's left edge,
    // x = blockLeft, on its own line).
    expect(
      geometry!.commaLeft,
      `comma left ${geometry!.commaLeft} must sit after the value's start (block left ${geometry!.blockLeft})`,
    ).toBeGreaterThan(geometry!.blockLeft)
  })

  test('copy still copies the FULL long string value when the tree truncates it', { tag: '@regression' }, async ({ page, env }) => {
    await loginAsAdmin(page, env)

    const longText = 'The quick brown fox jumps over the lazy dog. '.repeat(60)

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
    await page.route('**/api/v1/runs/run-json-viewer/events*', (route) => {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [] }) })
    })
    await page.route('**/api/v1/runs/run-json-viewer/io', (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          input_payload: { prompt: 'hello world', agent_stdout: longText },
          outputs_json: { format: { output: { result: 'success' } } },
        }),
      })
    })

    await page.goto('/runs/run-json-viewer')
    await expect(page.getByTestId('json-viewer-copy')).toBeVisible()
    await expect(page.getByTestId('json-viewer-string-truncated').first()).toBeVisible()

    await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
    await page.getByTestId('json-viewer-copy').click()
    const clipboard = await page.evaluate(() => navigator.clipboard.readText())
    expect(clipboard).toContain(longText)
  })
})
