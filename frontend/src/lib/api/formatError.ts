export interface ProblemDetail {
  type: string
  title: string
  status: number
  detail: string
  instance?: string
  request_id?: string
}

const PROBLEM_TITLES: Record<string, string> = {
  bad_request: 'Bad Request',
  validation_error: 'Validation Error',
  unauthorized: 'Unauthorized',
  forbidden: 'Forbidden',
  not_found: 'Not Found',
  conflict: 'Conflict',
  rate_limited: 'Rate Limited',
  feature_required: 'Feature Not Available',
  pipeline_error: 'Pipeline Error',
  migration_required: 'Migration Required',
  internal_error: 'Internal Error',
}

export function getProblemTypeLabel(type: string): string {
  const key = type.replace('urn:problem:modulo:', '')
  return PROBLEM_TITLES[key] ?? 'Error'
}

export function isProblemDetail(err: unknown): err is ProblemDetail {
  if (typeof err !== 'object' || err === null) return false
  const obj = err as Record<string, unknown>
  return (
    typeof obj.type === 'string' &&
    typeof obj.title === 'string' &&
    typeof obj.status === 'number' &&
    typeof obj.detail === 'string'
  )
}

export function toProblemDetail(err: unknown): ProblemDetail {
  if (isProblemDetail(err)) return err
  return {
    type: 'urn:problem:modulo:unknown',
    title: 'Error',
    status: 0,
    detail: formatApiError(err),
  }
}

export function formatApiError(err: unknown): string {
  if (isProblemDetail(err)) return err.detail
  if (typeof err === 'string') return err
  if (!err) return 'Unknown error'
  if (err instanceof Error) return err.message
  if (typeof err === 'object') {
    const obj = err as Record<string, unknown>
    if (typeof obj.detail === 'string') return obj.detail
    if (typeof obj.message === 'string') return obj.message
    if (typeof obj.error === 'string') return obj.error
    if (typeof obj.title === 'string') return obj.title
    try {
      return JSON.stringify(obj)
    } catch {
      return String(err)
    }
  }
  return String(err)
}
