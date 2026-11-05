/**
 * Canonical support reference text.
 *
 * A user who hits a failure has no single thing to send us: the run detail page
 * used to copy a "share summary" with no error code and only a truncated run
 * id, and the error dashboard had no copy affordance at all. This module is the
 * ONE place that builds the reference text, so the run and error surfaces can
 * never drift apart.
 *
 * Output is plain text, one field group per line, no prose — a support engineer
 * reads it as key/value pairs and can paste it into a ticket verbatim.
 */

export interface RunSupportReferenceInput {
  /** The run's FULL uuid — never a shortened display id. */
  runId: string
  pipelineId: string
  pipelineName?: string | null
  status: string
  errorCode?: string | null
}

export interface ErrorGroupSupportReferenceInput {
  groupId: string
  fingerprint: string
  level: string
  count: number
  /**
   * The reporting source (backend / frontend / saq / celery). The error-group
   * LIST payload does not carry it — only the sample event on the detail
   * payload does — so the segment is omitted when it is unavailable rather than
   * inventing a value.
   */
  source?: string | null
}

const FIELD_SEPARATOR = ' · '

/**
 * Resolve the public origin the SPA is served from. The app has no configured
 * public-base-URL env var; the deployment origin is already available at
 * runtime and runs are served on it at `/runs/<id>`, so that is what we use.
 * Returns null outside a browser (SSR / a DOM-less test) — the caller then
 * omits the url line rather than emitting a broken one.
 */
function withoutTrailingSlashes(value: string): string {
  let end = value.length
  while (end > 0 && value[end - 1] === '/') {
    end -= 1
  }
  return value.slice(0, end)
}

function resolveOrigin(origin?: string | null): string | null {
  if (origin) return withoutTrailingSlashes(origin)
  if (typeof window !== 'undefined' && window.location?.origin) {
    return withoutTrailingSlashes(window.location.origin)
  }
  return null
}

/**
 * Build the run support reference:
 *
 *   run <run_id> · pipeline <pipeline_id> (<name>) · status <status> · error <error_code>
 *   url <origin>/runs/<run_id>
 *
 * The parenthetical pipeline name is omitted when unknown, and `error` reads
 * `none` for a run that did not fail (no error_code). The url line is omitted
 * when no origin is available.
 */
export function buildRunSupportReference(
  input: RunSupportReferenceInput,
  origin?: string | null,
): string {
  const pipelineName = input.pipelineName?.trim()
  const pipeline = pipelineName
    ? `${input.pipelineId} (${pipelineName})`
    : input.pipelineId
  const errorCode = input.errorCode?.trim() || 'none'

  const lines = [
    [
      `run ${input.runId}`,
      `pipeline ${pipeline}`,
      `status ${input.status}`,
      `error ${errorCode}`,
    ].join(FIELD_SEPARATOR),
  ]

  const resolvedOrigin = resolveOrigin(origin)
  if (resolvedOrigin) {
    lines.push(`url ${resolvedOrigin}/runs/${input.runId}`)
  }

  return lines.join('\n')
}

/**
 * Build the error-group support reference:
 *
 *   error <group_id> · fingerprint <fingerprint> · level <level> · count <n> · source <source>
 *
 * The `source` segment is omitted when the caller has no source for the group
 * (the list payload does not carry one).
 */
export function buildErrorGroupSupportReference(
  input: ErrorGroupSupportReferenceInput,
): string {
  const fields = [
    `error ${input.groupId}`,
    `fingerprint ${input.fingerprint}`,
    `level ${input.level}`,
    `count ${input.count}`,
  ]
  const source = input.source?.trim()
  if (source) {
    fields.push(`source ${source}`)
  }
  return fields.join(FIELD_SEPARATOR)
}
