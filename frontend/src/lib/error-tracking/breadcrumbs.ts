import type { Breadcrumb } from './types'

let _collectorInstance: BreadcrumbCollector | null = null

export class BreadcrumbCollector {
  private buffer: Breadcrumb[] = []
  private maxSize: number
  private clickHandler: ((e: MouseEvent) => void) | null = null
  private origFetch: typeof window.fetch | null = null

  constructor(maxSize = 50) {
    this.maxSize = maxSize
    _collectorInstance = this
  }

  startAutoCapture(): void {
    this.autoCaptureClicks()
    this.autoCaptureApiCalls()
  }

  stopAutoCapture(): void {
    if (this.clickHandler) {
      document.removeEventListener('click', this.clickHandler, true)
      this.clickHandler = null
    }
    if (this.origFetch && window.fetch !== this.origFetch) {
      window.fetch = this.origFetch
      this.origFetch = null
    }
  }

  add(type: Breadcrumb['type'], data: Record<string, unknown>): void {
    if ((window as unknown as Record<string, unknown>).__MODULO_ERROR_TRACKING_DISABLED__) return
    this.buffer.push({ type, timestamp: new Date().toISOString(), data })
    if (this.buffer.length > this.maxSize) {
      this.buffer = this.buffer.slice(-this.maxSize)
    }
  }

  getBreadcrumbs(): Breadcrumb[] {
    return [...this.buffer]
  }

  clear(): void {
    this.buffer = []
  }

  captureApiCall(method: string, url: string, statusCode: number): void {
    this.add('api', { method: method.toUpperCase(), url, statusCode })
  }

  captureRouteChange(from: string | undefined, to: string | undefined): void {
    this.add('route_change', { from: from ?? '', to: to ?? '' })
  }

  private autoCaptureClicks(): void {
    this.clickHandler = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null
      if (!target) return
      const selector = getElementSelector(target)
      const text = (target.textContent?.trim() ?? '').slice(0, 80)
      this.add('click', { target: selector, text })
    }
    document.addEventListener('click', this.clickHandler, true)
  }

  private autoCaptureApiCalls(): void {
    if (typeof window.fetch !== 'function') return
    this.origFetch = window.fetch.bind(window)
    const self = this
    window.fetch = function (input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
      const url = typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : input.url
      const method = (init?.method ?? 'GET').toUpperCase()
      const fetchFn = self.origFetch!
      return fetchFn(input, init).then((response) => {
        self.captureApiCall(method, url, response.status)
        return response
      }).catch((err: unknown) => {
        self.captureApiCall(method, url, 0)
        throw err
      })
    }
  }
}

export function getCollector(): BreadcrumbCollector | null {
  return _collectorInstance
}

function getElementSelector(el: HTMLElement): string {
  if (el.id) return `#${el.id}`
  if (el.className && typeof el.className === 'string') {
    const cls = el.className.split(/\s+/).filter(Boolean).slice(0, 2).join('.')
    if (cls) return `${el.tagName.toLowerCase()}.${cls}`
  }
  return el.tagName.toLowerCase()
}
