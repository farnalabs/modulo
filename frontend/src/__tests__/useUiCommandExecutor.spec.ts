import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

describe('useUiCommandExecutor', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  describe('navigate', () => {
    it('calls router.push and returns URL', async () => {
      const mockPush = vi.fn()
      vi.doMock('vue-router', () => ({
        useRouter: () => ({ push: mockPush }),
      }))

      const { executeCommandBatch, abortUiCommands } = await import(
        '../../composables/useUiCommandExecutor'
      )
      const results = await executeCommandBatch([
        { id: 'nav-1', name: 'navigate', args: { path: '/admin/pipelines' } },
      ])
      expect(results[0].success).toBe(true)
      expect(results[0].name).toBe('navigate')
      expect(results[0].result).toBeDefined()
    })
  })

  describe('click', () => {
    it('finds element by data-testid and clicks it', async () => {
      const btn = document.createElement('button')
      btn.setAttribute('data-testid', 'save-btn')
      document.body.appendChild(btn)

      const clickSpy = vi.spyOn(btn, 'click')

      const { executeCommandBatch } = await import(
        '../../composables/useUiCommandExecutor'
      )
      const results = await executeCommandBatch([
        { id: 'click-1', name: 'click', args: { selector: '[data-testid=save-btn]' } },
      ])
      expect(results[0].success).toBe(true)
      expect(clickSpy).toHaveBeenCalledOnce()
    })

    it('returns error when element not found', async () => {
      const { executeCommandBatch } = await import(
        '../../composables/useUiCommandExecutor'
      )
      const results = await executeCommandBatch([
        { id: 'click-1', name: 'click', args: { selector: '.non-existent' } },
      ])
      expect(results[0].success).toBe(false)
      expect(results[0].error).toContain('Element not found')
    })
  })

  describe('fill', () => {
    it('dispatches input/change events on native input', async () => {
      const input = document.createElement('input')
      input.setAttribute('data-testid', 'name-input')
      input.type = 'text'
      document.body.appendChild(input)

      const inputSpy = vi.fn()
      const changeSpy = vi.fn()
      input.addEventListener('input', inputSpy)
      input.addEventListener('change', changeSpy)

      const { executeCommandBatch } = await import(
        '../../composables/useUiCommandExecutor'
      )
      const results = await executeCommandBatch([
        { id: 'fill-1', name: 'fill', args: { selector: '[data-testid=name-input]', value: 'test value' } },
      ])
      expect(results[0].success).toBe(true)
      expect(inputSpy).toHaveBeenCalled()
      expect(changeSpy).toHaveBeenCalled()
    })
  })

  describe('extract', () => {
    it('returns element textContent', async () => {
      const div = document.createElement('div')
      div.setAttribute('data-testid', 'output')
      div.textContent = 'Hello World'
      document.body.appendChild(div)

      const { executeCommandBatch } = await import(
        '../../composables/useUiCommandExecutor'
      )
      const results = await executeCommandBatch([
        { id: 'extract-1', name: 'extract', args: { selector: '[data-testid=output]' } },
      ])
      expect(results[0].success).toBe(true)
      expect(results[0].result?.text).toBe('Hello World')
    })
  })

  describe('get_page_interactables', () => {
    it('returns interactive elements', async () => {
      const btn = document.createElement('button')
      btn.textContent = 'Click Me'
      btn.setAttribute('data-testid', 'action-btn')
      document.body.appendChild(btn)

      const { executeCommandBatch } = await import(
        '../../composables/useUiCommandExecutor'
      )
      const results = await executeCommandBatch([
        { id: 'gi-1', name: 'get_page_interactables', args: {} },
      ])
      expect(results[0].success).toBe(true)
      expect(results[0].result?.count).toBeGreaterThanOrEqual(1)
      expect(results[0].result?.items[0].testid).toBe('action-btn')
    })
  })

  describe('wait', () => {
    it('resolves after specified ms', async () => {
      const { executeCommandBatch } = await import(
        '../../composables/useUiCommandExecutor'
      )
      const promise = executeCommandBatch([
        { id: 'wait-1', name: 'wait', args: { ms: 100 } },
      ])

      await vi.advanceTimersByTimeAsync(200)

      const results = await promise
      expect(results[0].success).toBe(true)
    })

    it('polls until selector appears', async () => {
      const { executeCommandBatch } = await import(
        '../../composables/useUiCommandExecutor'
      )
      const promise = executeCommandBatch([
        { id: 'wait-1', name: 'wait', args: { selector: '[data-testid=late-element]', timeout: 5000 } },
      ])

      const el = document.createElement('div')
      el.setAttribute('data-testid', 'late-element')
      setTimeout(() => document.body.appendChild(el), 300)

      await vi.advanceTimersByTimeAsync(2000)

      const results = await promise
      expect(results[0].success).toBe(true)
    })
  })

  describe('abort', () => {
    it('returns cancelled_by_user for remaining commands', async () => {
      const { executeCommandBatch, abortUiCommands } = await import(
        '../../composables/useUiCommandExecutor'
      )

      const promise = executeCommandBatch([
        { id: 'nav-1', name: 'navigate', args: { path: '/admin' } },
        { id: 'click-1', name: 'click', args: { selector: '.btn' } },
        { id: 'fill-1', name: 'fill', args: { selector: '#input', value: 'x' } },
      ])

      abortUiCommands()
      await vi.advanceTimersByTimeAsync(100)

      const results = await promise
      expect(results.length).toBe(3)
      const cancelled = results.filter(r => r.error === 'cancelled_by_user')
      expect(cancelled.length).toBeGreaterThanOrEqual(1)
    })
  })

  describe('resolveElement', () => {
    it('tries data-testid first, then CSS fallback', async () => {
      const div = document.createElement('div')
      div.setAttribute('data-testid', 'my-element')
      div.className = 'fallback'
      document.body.appendChild(div)

      const { executeCommandBatch } = await import(
        '../../composables/useUiCommandExecutor'
      )
      const results = await executeCommandBatch([
        { id: 'click-1', name: 'click', args: { selector: 'my-element' } },
      ])
      expect(results[0].success).toBe(true)

      const results2 = await executeCommandBatch([
        { id: 'click-2', name: 'click', args: { selector: '.fallback' } },
      ])
      expect(results2[0].success).toBe(true)
    })
  })
})
