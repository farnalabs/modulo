/**
 * Shared request-deadline helper for the typed API client.
 *
 * The typed `api` client attaches no deadline of its own, so every call site
 * that needs one uses this helper: it invokes a factory with an `AbortSignal`
 * and ABORTS the underlying fetch when the deadline elapses (or when an
 * optional caller-owned `signal` aborts, e.g. a superseded request or a
 * component unmount). Both the deadline and the caller's cancellation share one
 * controller, so a single abort path serves the request.
 *
 * Distinct from `lib/asyncUtils.ts`'s promise-race `withTimeout(promise, ms,
 * label)`, which cannot abort the request it races. This factory form lived
 * locally in `views/PipelineEditorView.vue`; it moved here (FAR-1818) so the
 * version-compare component reuses the established pattern instead of shipping
 * a third copy.
 */
export const DEFAULT_REQUEST_TIMEOUT_MS = 15000

/** Raised when the request deadline elapses before the factory settles. */
export class RequestTimeoutError extends Error {
  constructor(ms: number) {
    super(`Request timed out after ${ms}ms`)
    this.name = 'RequestTimeoutError'
  }
}

export function withTimeout<T>(
  factory: (signal: AbortSignal) => Promise<T>,
  ms: number = DEFAULT_REQUEST_TIMEOUT_MS,
  externalSignal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort()
    else externalSignal.addEventListener('abort', abort, { once: true })
  }
  let timer: ReturnType<typeof setTimeout> | undefined
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort()
      reject(new RequestTimeoutError(ms))
    }, ms)
  })
  // A factory may throw SYNCHRONOUSLY (before returning a promise) rather than
  // rejecting — e.g. a malformed argument or a programming error raised while
  // building the request. That path must still clear the deadline timer, so the
  // setup is wrapped instead of relying on the promise chain's `.finally`.
  let request: Promise<T>
  try {
    request = factory(controller.signal)
  } catch (e: unknown) {
    clearTimeout(timer)
    deadline.catch(() => {})
    externalSignal?.removeEventListener('abort', abort)
    return Promise.reject(e)
  }
  // Swallow whichever side of the race loses, so a factory that rejects on
  // abort AFTER the deadline already settled the race is never an unhandled
  // rejection.
  request.catch(() => {})
  deadline.catch(() => {})
  return Promise.race([request, deadline]).finally(() => {
    clearTimeout(timer)
    externalSignal?.removeEventListener('abort', abort)
  })
}
