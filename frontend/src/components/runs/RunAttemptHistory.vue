<template>
  <!-- FAR-2078: the durable per-attempt execution history of a run. This is a
       STATIC record of what already happened, not a live status surface, so it
       is deliberately NOT an aria-live region — a screen reader should read it
       on navigation, not be interrupted by it. The section always renders: a
       run with no recorded attempts (older runs) says so plainly, and a failed
       / 404 fetch shows an inline unavailable state rather than breaking the
       rest of the run page. -->
  <DataCard
    class="mb-6"
    data-testid="run-attempt-history"
    :title="$t('components.RunAttemptHistory.title')"
    :description="$t('components.RunAttemptHistory.help')"
    heading-level="h2"
    title-class="text-base font-semibold tracking-tight"
    description-class="text-xs text-muted-foreground"
    padding="lg"
  >
    <p v-if="loading" data-testid="run-attempt-history-loading" class="mt-3 text-sm text-muted-foreground">
      {{ $t('components.RunAttemptHistory.loading') }}
    </p>

    <div
      v-else-if="unavailable"
      data-testid="run-attempt-history-unavailable"
      class="mt-3 rounded-md border border-border bg-muted/30 p-3 text-sm text-muted-foreground"
    >
      <p>{{ $t('components.RunAttemptHistory.unavailable') }}</p>
      <button
        type="button"
        data-testid="run-attempt-history-retry"
        class="mt-2 rounded-md border border-input bg-background px-3 py-1 text-xs font-medium text-foreground transition-colors hover:bg-accent"
        @click="load"
      >
        {{ $t('components.RunAttemptHistory.retry') }}
      </button>
    </div>

    <p v-else-if="attempts.length === 0" data-testid="run-attempt-history-empty" class="mt-3 text-sm text-muted-foreground">
      {{ $t('components.RunAttemptHistory.empty') }}
    </p>

    <template v-else>
      <div class="mt-3 overflow-x-auto">
        <table data-testid="run-attempt-history-table" class="w-full text-left text-sm">
          <caption class="sr-only">{{ $t('components.RunAttemptHistory.table_caption') }}</caption>
          <thead>
            <tr class="border-b text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" class="py-2 pr-4 font-medium">{{ $t('components.RunAttemptHistory.column_attempt') }}</th>
              <th scope="col" class="py-2 pr-4 font-medium">{{ $t('components.RunAttemptHistory.column_started') }}</th>
              <th scope="col" class="py-2 pr-4 font-medium">{{ $t('components.RunAttemptHistory.column_duration') }}</th>
              <th scope="col" class="py-2 pr-4 font-medium">{{ $t('components.RunAttemptHistory.column_terminal_reason') }}</th>
              <th scope="col" class="py-2 pr-4 font-medium">{{ $t('components.RunAttemptHistory.column_model') }}</th>
              <th scope="col" class="py-2 font-medium">{{ $t('components.RunAttemptHistory.column_error_code') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="attempt in attempts"
              :key="attempt.attempt_index"
              data-testid="run-attempt-history-row"
              class="border-b border-border/60 last:border-0"
            >
              <th scope="row" class="py-2 pr-4 text-left font-medium tabular-nums">{{ attemptNumber(attempt.attempt_index) }}</th>
              <td class="py-2 pr-4">
                <time :datetime="attempt.started_at">{{ formatStarted(attempt.started_at) }}</time>
              </td>
              <td class="py-2 pr-4">{{ durationText(attempt) }}</td>
              <td class="py-2 pr-4">{{ terminalReasonText(attempt) }}</td>
              <td class="py-2 pr-4 font-mono text-xs">{{ attempt.model_used || '—' }}</td>
              <td class="py-2 font-mono text-xs">{{ attempt.error_code || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="truncated" class="mt-2 text-xs text-muted-foreground">
        {{ $t('components.RunAttemptHistory.truncated', { shown: attempts.length, total }) }}
      </p>
    </template>
  </DataCard>
</template>

<script setup lang="ts">
import DataCard from '@/components/ui/DataCard.vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '@/lib/api/client'
import type { components } from '@/lib/api/client'
import { formatDateShortWithTime, toDate } from '@/lib/formatDate'
import { formatCompactDuration } from '@/lib/worstCase'

type RunAttemptItem = components['schemas']['RunAttemptItem']

/**
 * The run whose attempt history to render. The component fetches for itself so
 * the run detail view only needs a single mount point (one import + one tag) —
 * keeping the parent's diff minimal.
 */
const props = defineProps<{ runId: string }>()

const { t } = useI18n()

const attempts = ref<RunAttemptItem[]>([])
const total = ref(0)
const loading = ref(false)
const unavailable = ref(false)

/**
 * A monotonic request id guards against a stale response overwriting a newer
 * one when the run id changes mid-flight (the child is re-mounted per run, but
 * a rapid route change can leave two fetches in flight).
 */
let requestId = 0

async function load(): Promise<void> {
  const current = ++requestId
  loading.value = true
  unavailable.value = false
  try {
    const { data, error } = await api.GET('/api/v1/runs/{run_id}/attempts', {
      params: { path: { run_id: props.runId } },
    })
    if (current !== requestId) return
    // A 404 (cross-tenant / missing run) and a generic failure land in the
    // same inline state: the section is auxiliary, so it degrades to
    // "unavailable" rather than taking the whole run page down with it.
    if (error || !data) {
      attempts.value = []
      total.value = 0
      unavailable.value = true
      return
    }
    attempts.value = data.items ?? []
    total.value = data.total ?? attempts.value.length
  } catch {
    if (current !== requestId) return
    attempts.value = []
    total.value = 0
    unavailable.value = true
  } finally {
    if (current === requestId) loading.value = false
  }
}

onMounted(load)
watch(() => props.runId, load)

const truncated = computed<boolean>(() => total.value > attempts.value.length)

/** 1-based attempt number for display; the wire value is 0-based. */
function attemptNumber(index: number): number {
  return index + 1
}

function formatStarted(startedAt: string | null | undefined): string {
  return formatDateShortWithTime(startedAt)
}

/**
 * Attempt duration, or the open-attempt marker when the attempt has not ended
 * (still running, or parked awaiting a human). An unparseable interval renders
 * the dash placeholder rather than a fabricated duration.
 */
function durationText(attempt: RunAttemptItem): string {
  if (!attempt.ended_at) return t('components.RunAttemptHistory.running')
  const start = toDate(attempt.started_at)
  const end = toDate(attempt.ended_at)
  if (!start || !end || end.getTime() < start.getTime()) return '—'
  return formatCompactDuration((end.getTime() - start.getTime()) / 1000)
}

const TERMINAL_REASON_KEYS: Record<string, string> = {
  deadline: 'components.RunAttemptHistory.terminal_reason.deadline',
  stall: 'components.RunAttemptHistory.terminal_reason.stall',
  guard_cap: 'components.RunAttemptHistory.terminal_reason.guard_cap',
  worker_lost: 'components.RunAttemptHistory.terminal_reason.worker_lost',
  provider_error: 'components.RunAttemptHistory.terminal_reason.provider_error',
  completed: 'components.RunAttemptHistory.terminal_reason.completed',
  reset: 'components.RunAttemptHistory.terminal_reason.reset',
}

/**
 * The terminal reason in human-readable text. The wire vocabulary is closed
 * (enforced by the DB CHECK constraint), but an unrecognised value still maps
 * to a translated "Unknown" rather than leaking the raw enum into the UI.
 * An attempt with no recorded reason (still open, or ended without one) is
 * honestly a dash — it hasn't terminated for a recorded reason.
 */
function terminalReasonText(attempt: RunAttemptItem): string {
  if (!attempt.terminal_reason) return '—'
  const key = TERMINAL_REASON_KEYS[attempt.terminal_reason]
  return t(key ?? 'components.RunAttemptHistory.terminal_reason.unknown')
}
</script>
