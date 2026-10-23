/**
 * Worst-case wall-clock arithmetic for a node timeout (FAR-2072).
 *
 * A node's ``timeout_seconds`` reads as "this node gets N seconds", but a
 * timeout can be retried by the run-level ``retry_policy``: the whole run is
 * re-dispatched and the node receives its budget again. The ceiling is
 * therefore ``timeout_seconds x attempts`` where ``attempts`` is derived from
 * the pipeline's ACTUAL policy — never a hard-coded multiplier.
 *
 * The matcher mirrors the executor's ``_retry_after_policy`` exactly:
 *  - an absent/null ``on`` with ``max_retries > 0`` retries EVERY retryable
 *    event (stall, timeout, failure, eval_failed);
 *  - an explicit non-empty list retries only its members;
 *  - an explicit empty list (or a malformed value) retries nothing.
 *
 * Queue and dispatch time is deliberately outside this calculation — it is
 * the other large contributor to observed wall clock and is not bounded by
 * the node timeout.
 */

export type RetryableEvent = 'stall' | 'timeout' | 'failure' | 'eval_failed'

/** The retry-policy fields this module understands. */
export interface RetryPolicyLike {
  on?: unknown
  max_retries?: unknown
}

export interface WorstCase {
  /** Total attempts allowed for the event (the first attempt + retries). */
  attempts: number
  /** The node's own timeout, in seconds. */
  timeoutSeconds: number
  /** ``attempts x timeoutSeconds`` — the worst case, before queue time. */
  totalSeconds: number
}

const MAX_RETRIES = 5

function asPolicy(policy: unknown): RetryPolicyLike | null {
  if (!policy || typeof policy !== 'object' || Array.isArray(policy)) return null
  return policy as RetryPolicyLike
}

/**
 * How many attempts a policy allows for ``event`` (1 when nothing retries it).
 *
 * Mirrors ``modulo.core.pipeline_engine.executor._retry_after_policy``:
 * a malformed budget, an out-of-range budget, a non-integer budget, or an
 * explicit empty/malformed ``on`` list all fail closed to a single attempt.
 */
export function retryAttemptsForEvent(policy: unknown, event: RetryableEvent): number {
  const parsed = asPolicy(policy)
  if (!parsed) return 1

  const budget = parsed.max_retries
  if (typeof budget !== 'number' || !Number.isInteger(budget) || budget <= 0 || budget > MAX_RETRIES) {
    return 1
  }

  const on = parsed.on
  // Absent or explicitly-null `on` = all retryable events.
  if (on === undefined || on === null) return budget + 1
  // Explicit empty list, or malformed non-list, fails closed (no retry).
  if (!Array.isArray(on) || on.length === 0) return 1
  return on.includes(event) ? budget + 1 : 1
}

/**
 * Derive the worst-case arithmetic for one node timeout event.
 *
 * Returns ``null`` when the timeout is missing/non-positive (nothing to
 * compute), so a caller never renders "0s x 0".
 */
export function worstCaseForEvent(
  policy: unknown,
  timeoutSeconds: unknown,
  event: RetryableEvent = 'timeout',
): WorstCase | null {
  if (
    typeof timeoutSeconds !== 'number' ||
    !Number.isFinite(timeoutSeconds) ||
    timeoutSeconds <= 0
  ) {
    return null
  }
  const attempts = retryAttemptsForEvent(policy, event)
  return { attempts, timeoutSeconds, totalSeconds: timeoutSeconds * attempts }
}

/** Compact, locale-neutral duration: ``45s``, ``55m``, ``1m30s``, ``2h45m``. */
export function formatCompactDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.round(totalSeconds))
  if (seconds < 60) return `${seconds}s`

  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainder = seconds % 60

  if (hours > 0) {
    if (minutes > 0 && remainder > 0) return `${hours}h${minutes}m${remainder}s`
    if (minutes > 0) return `${hours}h${minutes}m`
    if (remainder > 0) return `${hours}h${remainder}s`
    return `${hours}h`
  }
  if (remainder > 0) return `${minutes}m${remainder}s`
  return `${minutes}m`
}

export interface AttemptElapsed {
  /** 1-based attempt number. */
  attempt: number
  /** Elapsed seconds, or null when the data cannot resolve an interval. */
  seconds: number | null
}

export interface AttemptTimelineInput {
  attempts: number
  startedAt?: unknown
  dispatchedAt?: unknown
  completedAt?: unknown
  /** Clock for a running attempt; defaults to ``Date.now()``. */
  now?: number
}

function parseInstant(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'string' || value.length === 0) return null
  const millis = Date.parse(value)
  return Number.isFinite(millis) ? millis : null
}

/**
 * Build the per-attempt elapsed rows we can actually observe.
 *
 * ``started_at`` marks the FIRST attempt's start; ``dispatched_at`` is
 * rewritten on every claim, so it marks the LATEST attempt's start. That gives
 * an exact interval for attempt 1 and for the latest attempt; any attempts in
 * between have no start clock exposed by the API and are reported as null
 * rather than guessed.
 */
export function attemptElapsedRows(input: AttemptTimelineInput): AttemptElapsed[] {
  const rawAttempts = Number(input.attempts)
  const attempts =
    Number.isFinite(rawAttempts) && rawAttempts >= 1 ? Math.floor(rawAttempts) : 1

  const started = parseInstant(input.startedAt)
  const dispatched = parseInstant(input.dispatchedAt)
  const completed = parseInstant(input.completedAt)
  const now = typeof input.now === 'number' && Number.isFinite(input.now) ? input.now : Date.now()
  const firstStart = started ?? dispatched
  const lastEnd = completed ?? now

  const rows: AttemptElapsed[] = []
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    let start: number | null = null
    let end: number | null = null

    if (attempts === 1) {
      start = firstStart
      end = lastEnd
    } else if (attempt === 1) {
      start = firstStart
      end = dispatched
    } else if (attempt === attempts) {
      start = dispatched
      end = lastEnd
    }

    const seconds = start !== null && end !== null && end >= start ? Math.round((end - start) / 1000) : null
    rows.push({ attempt, seconds })
  }
  return rows
}

/** Parse the trailing attempt number from ``run:<id>:node:<id>:<n>``. */
export function parseAttemptNumber(attemptKey: unknown): number | null {
  if (typeof attemptKey !== 'string') return null
  const match = /:(\d+)$/.exec(attemptKey)
  if (!match) return null
  const value = Number(match[1])
  return Number.isInteger(value) && value > 0 ? value : null
}
