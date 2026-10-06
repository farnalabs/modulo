/**
 * Error thrown by `useApi` for a non-2xx response. Carries the HTTP status and,
 * when the backend supplied one, the RFC 9457 `code` extension member so
 * callers can discriminate without substring-matching the human message.
 *
 * Kept in its own module (rather than `formatError.ts`) so it is never a
 * casualty of the many specs that partially mock `formatError`.
 */
export class ApiError extends Error {
  readonly status: number
  readonly code?: string

  constructor(message: string, status: number, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

/** Extract the RFC 9457 `code` extension member from a response body, if present. */
export function getProblemCode(body: unknown): string | undefined {
  if (typeof body !== 'object' || body === null) return undefined
  const code = (body as { code?: unknown }).code
  return typeof code === 'string' ? code : undefined
}

/** The machine-readable error code carried by a thrown `ApiError`, if any. */
export function getApiErrorCode(err: unknown): string | undefined {
  return err instanceof ApiError && typeof err.code === 'string' ? err.code : undefined
}
