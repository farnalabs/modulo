import { getCurrentInstance, onBeforeUnmount, ref } from 'vue'

/**
 * Canonical support references (FAR-2304).
 *
 * A support reference is the ONE thing a user copies when they need to ask for
 * help: it carries the full identifiers an operator needs to find the run or
 * the error group, plus a link to it. It is built here - in one place - so the
 * run detail page and the error dashboard always produce an identical shape.
 *
 * Shape (one line per field group, no prose):
 *
 *   run <run_id> · pipeline <pipeline_id> (<name>) · status <status> · error <error_code>
 *   url <base>/runs/<run_id>
 *
 *   error <group_id> · fingerprint <fingerprint> · status <status> · level <level> · count <count>
 *   url <base>/admin/errors/<group_id>
 *
 * A field whose value is unavailable is OMITTED - never rendered as a
 * placeholder. An em dash in a copied reference reads as data to whoever
 * receives it, which is exactly what a support reference must not do.
 */

/** Translator shape: the `t` returned by `useI18n()`. */
export type SupportReferenceTranslator = (key: string) => string

export interface RunSupportReferenceSource {
  run_id?: string | null
  pipeline_id?: string | null
  pipeline_name?: string | null
  status?: string | null
  error_code?: string | null
}

export interface ErrorGroupSupportReferenceSource {
  id?: string | null
  fingerprint?: string | null
  status?: string | null
  level_peak?: string | null
  count?: number | null
}

export interface SupportReferenceOptions {
  /** The view's translator, so every label is localised. */
  t: SupportReferenceTranslator
  /** Public origin the reference should link to. Defaults to the page origin. */
  baseUrl?: string
  /**
   * Error groups live in two partitions: an instance-scope group is only
   * reachable with `?scope=instance`, so a copied link must carry it or the
   * recipient lands on a 404.
   */
  scope?: 'organisation' | 'instance'
}

const FIELD_SEPARATOR = ' · '
const COPIED_FEEDBACK_MS = 2000

const LABEL_KEYS = {
  run: 'common.support_reference.run',
  pipeline: 'common.support_reference.pipeline',
  status: 'common.support_reference.status',
  error: 'common.support_reference.error',
  url: 'common.support_reference.url',
  fingerprint: 'common.support_reference.fingerprint',
  level: 'common.support_reference.level',
  count: 'common.support_reference.count',
} as const

function clean(value: string | number | null | undefined): string | null {
  if (value === null || value === undefined) return null
  const text = String(value).trim()
  return text === '' ? null : text
}

function field(
  t: SupportReferenceTranslator,
  labelKey: string,
  value: string | number | null | undefined,
): string | null {
  const text = clean(value)
  return text === null ? null : `${t(labelKey)} ${text}`
}

function joinFields(fields: Array<string | null>): string {
  return fields.filter((entry): entry is string => entry !== null).join(FIELD_SEPARATOR)
}

/** The public origin a reference links to, without a trailing slash. */
export function supportReferenceBaseUrl(baseUrl?: string): string {
  const pageOrigin = typeof window === 'undefined' ? null : window.location.origin
  const resolved = clean(baseUrl) ?? clean(pageOrigin) ?? ''
  return resolved.replace(/\/+$/, '')
}

/** Build the canonical support reference for one run. Empty when it has no id. */
export function buildRunSupportReference(
  run: RunSupportReferenceSource,
  options: SupportReferenceOptions,
): string {
  const runId = clean(run.run_id)
  if (runId === null) return ''
  const { t } = options

  const pipelineId = clean(run.pipeline_id)
  const pipelineName = clean(run.pipeline_name)
  const pipeline = pipelineId === null
    ? null
    : pipelineName === null
      ? pipelineId
      : `${pipelineId} (${pipelineName})`

  const details = joinFields([
    field(t, LABEL_KEYS.run, runId),
    field(t, LABEL_KEYS.pipeline, pipeline),
    field(t, LABEL_KEYS.status, run.status),
    field(t, LABEL_KEYS.error, run.error_code),
  ])
  const url = `${supportReferenceBaseUrl(options.baseUrl)}/runs/${runId}`
  return `${details}\n${t(LABEL_KEYS.url)} ${url}`
}

/** Build the canonical support reference for one error group. Empty when it has no id. */
export function buildErrorGroupSupportReference(
  group: ErrorGroupSupportReferenceSource,
  options: SupportReferenceOptions,
): string {
  const groupId = clean(group.id)
  if (groupId === null) return ''
  const { t } = options

  const details = joinFields([
    field(t, LABEL_KEYS.error, groupId),
    field(t, LABEL_KEYS.fingerprint, group.fingerprint),
    field(t, LABEL_KEYS.status, group.status),
    field(t, LABEL_KEYS.level, group.level_peak),
    field(t, LABEL_KEYS.count, group.count),
  ])
  const scope = options.scope === 'instance' ? '?scope=instance' : ''
  const url = `${supportReferenceBaseUrl(options.baseUrl)}/admin/errors/${groupId}${scope}`
  return `${details}\n${t(LABEL_KEYS.url)} ${url}`
}

/**
 * Copy affordance shared by every support-reference surface: copies the
 * reference the user triggered, reports success (or failure) so the caller can
 * render visible + announced feedback, and clears the success flag after a
 * short window.
 */
export function useSupportReference() {
  const copied = ref(false)
  const copyFailed = ref(false)
  let resetTimer: ReturnType<typeof setTimeout> | null = null

  function clearResetTimer() {
    if (resetTimer !== null) {
      clearTimeout(resetTimer)
      resetTimer = null
    }
  }

  async function copyReference(text: string): Promise<boolean> {
    if (!text) return false
    copyFailed.value = false
    try {
      await navigator.clipboard.writeText(text)
    } catch (error) {
      // A refused clipboard is a real failure: surface it rather than letting
      // the user believe the reference is on their clipboard.
      copyFailed.value = true
      console.warn('[useSupportReference] failed to copy the support reference', error)
      return false
    }
    copied.value = true
    clearResetTimer()
    resetTimer = setTimeout(() => {
      copied.value = false
      resetTimer = null
    }, COPIED_FEEDBACK_MS)
    return true
  }

  // Guard so the shared helper can be exercised outside a component (e.g. a
  // unit test) without Vue warning about a missing instance.
  if (getCurrentInstance()) {
    onBeforeUnmount(clearResetTimer)
  }

  return { copied, copyFailed, copyReference }
}
