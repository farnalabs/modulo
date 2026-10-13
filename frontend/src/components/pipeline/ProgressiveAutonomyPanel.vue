<template>
  <!-- FAR-1696 slice (b): progressive-autonomy visibility on the pipeline
       editor (ADR 043 slice S5's frontend surface over the FAR-1175
       earned-autonomy state). Read-only display of default / ceiling / earned
       plus manual promote/demote, wired to the already-live REST endpoints.
       No backend change: the panel reads the levels off the loaded pipeline
       and refetches via the `changed` event. -->
  <section
    class="border-b bg-card px-3 py-2"
    data-testid="progressive-autonomy-panel"
    :aria-label="$t('views.PipelineEditorView.progressive_autonomy_title')"
  >
    <div class="flex flex-wrap items-center gap-x-4 gap-y-1">
      <h3 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {{ $t('views.PipelineEditorView.progressive_autonomy_title') }}
      </h3>
      <dl class="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px]">
        <div class="flex items-center gap-1">
          <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.progressive_autonomy_default') }}</dt>
          <dd class="font-medium text-foreground">{{ levelLabel(defaultLevel) }}</dd>
        </div>
        <div class="flex items-center gap-1">
          <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.progressive_autonomy_ceiling') }}</dt>
          <dd class="font-medium text-foreground">{{ levelLabel(effectiveCeiling) }}</dd>
        </div>
        <div class="flex items-center gap-1">
          <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.progressive_autonomy_earned') }}</dt>
          <dd class="font-medium text-foreground">
            <span data-testid="progressive-autonomy-earned">{{ earnedLabel }}</span>
            <span
              v-if="hasEarned && earnedUpdatedAt"
              class="ml-1 text-[10px] text-muted-foreground"
            >{{ $t('views.PipelineEditorView.progressive_autonomy_updated_at', { date: formattedUpdatedAt }) }}</span>
          </dd>
        </div>
      </dl>
    </div>

    <p class="mt-1 text-[10px] text-muted-foreground">
      {{ $t('views.PipelineEditorView.progressive_autonomy_gating_note') }}
    </p>

    <!-- Manual promote/demote. Each button moves the earned level one rung;
         a button that would violate direction or the ceiling is disabled
         (the server still enforces and answers 422). -->
    <div class="mt-2 flex flex-wrap items-end gap-2">
      <label class="flex flex-col gap-0.5 text-[10px] text-muted-foreground">
        {{ $t('views.PipelineEditorView.progressive_autonomy_reason_label') }}
        <input
          v-model="reason"
          type="text"
          class="w-64 rounded-md border border-input bg-background px-1.5 py-1 text-xs placeholder:text-muted-foreground"
          :placeholder="$t('views.PipelineEditorView.progressive_autonomy_reason_placeholder')"
          data-testid="progressive-autonomy-reason"
        />
      </label>
      <button
        type="button"
        :class="btnSecondary"
        :disabled="!canDemote || busy !== null"
        :title="demoteTitle"
        data-testid="progressive-autonomy-demote"
        @click="change('demote')"
      >
        {{ $t('views.PipelineEditorView.progressive_autonomy_demote') }}
      </button>
      <button
        type="button"
        :class="btnSecondary"
        :disabled="!canPromote || busy !== null"
        :title="promoteTitle"
        data-testid="progressive-autonomy-promote"
        @click="change('promote')"
      >
        {{ $t('views.PipelineEditorView.progressive_autonomy_promote') }}
      </button>
    </div>

    <p
      role="status"
      aria-live="polite"
      class="mt-1 text-[10px] text-muted-foreground"
      data-testid="progressive-autonomy-status"
    >{{ busy === null ? statusMessage : $t('views.PipelineEditorView.progressive_autonomy_applying') }}</p>

    <p
      v-if="errorMessage"
      role="alert"
      aria-live="assertive"
      class="mt-1 text-xs text-destructive"
      data-testid="progressive-autonomy-error"
    >{{ errorMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../../lib/api/client'
import { formatApiError, isProblemDetail } from '../../lib/api/formatError'
import { formatDateShortWithTime } from '../../lib/formatDate'

interface Props {
  pipelineId: string
  defaultLevel?: string | null
  /** Explicit ceiling; null = inherit (effective ceiling is the default). */
  maxLevel?: string | null
  /** Runtime earned level; null until a promote/demote sets it. */
  earnedLevel?: string | null
  earnedUpdatedAt?: string | null
}

const props = defineProps<Props>()
const emit = defineEmits<{ (e: 'changed'): void }>()

const { t } = useI18n()

// Canonical ladder: manual_approval < notify_on_complete < fully_autonomous.
// Unknown / null values fall back to the pinned default (manual_approval),
// matching the backend's effective-level resolution.
const AUTONOMY_LEVELS = ['manual_approval', 'notify_on_complete', 'fully_autonomous'] as const
const BASE_FALLBACK = 'manual_approval'

function rank(level: string | null | undefined): number {
  const index = (AUTONOMY_LEVELS as readonly string[]).indexOf(level ?? '')
  return index === -1 ? 0 : index
}

function levelLabel(level: string | null | undefined): string {
  const canonical = AUTONOMY_LEVELS[rank(level)]
  if (canonical === 'notify_on_complete') return t('views.PipelineEditorView.autonomy_notify_on_complete')
  if (canonical === 'fully_autonomous') return t('views.PipelineEditorView.autonomy_fully_autonomous')
  return t('views.PipelineEditorView.autonomy_manual_approval')
}

const reason = ref('')
const busy = ref<'promote' | 'demote' | null>(null)
const statusMessage = ref('')
const errorMessage = ref<string | null>(null)

const hasEarned = computed(() => Boolean(props.earnedLevel))
// An explicit `max_autonomy_level` wins; when unset the effective ceiling is
// the pipeline default (backend `_effective_ceiling`).
const effectiveCeiling = computed(() => props.maxLevel ?? props.defaultLevel ?? BASE_FALLBACK)
const currentLevel = computed(() => props.earnedLevel ?? props.defaultLevel ?? BASE_FALLBACK)
const currentRank = computed(() => rank(currentLevel.value))
const ceilingRank = computed(() => rank(effectiveCeiling.value))

const demoteTarget = computed(() =>
  currentRank.value > 0 ? AUTONOMY_LEVELS[currentRank.value - 1] : null,
)
const promoteTarget = computed(() => {
  const next = currentRank.value + 1
  if (next >= AUTONOMY_LEVELS.length) return null
  if (next > ceilingRank.value) return null
  return AUTONOMY_LEVELS[next] ?? null
})
const canDemote = computed(() => demoteTarget.value !== null)
const canPromote = computed(() => promoteTarget.value !== null)

const earnedLabel = computed(() =>
  hasEarned.value
    ? levelLabel(props.earnedLevel)
    : t('views.PipelineEditorView.progressive_autonomy_not_set', { level: levelLabel(props.defaultLevel) }),
)
const formattedUpdatedAt = computed(() =>
  props.earnedUpdatedAt ? formatDateShortWithTime(props.earnedUpdatedAt) : '',
)
const demoteTitle = computed(() =>
  canDemote.value
    ? t('views.PipelineEditorView.progressive_autonomy_demote_hint', { level: levelLabel(demoteTarget.value) })
    : t('views.PipelineEditorView.progressive_autonomy_at_floor'),
)
const promoteTitle = computed(() =>
  canPromote.value
    ? t('views.PipelineEditorView.progressive_autonomy_promote_hint', { level: levelLabel(promoteTarget.value) })
    : t('views.PipelineEditorView.progressive_autonomy_at_ceiling'),
)

function autonomyErrorMessage(error: unknown, status: number | undefined): string {
  const forbidden = status === 403 || (isProblemDetail(error) && error.status === 403)
  if (forbidden) {
    return t('views.PipelineEditorView.progressive_autonomy_forbidden', { error: formatApiError(error) })
  }
  if (status === 422) {
    return t('views.PipelineEditorView.progressive_autonomy_rejected', { error: formatApiError(error) })
  }
  return t('views.PipelineEditorView.progressive_autonomy_failed', { error: formatApiError(error) })
}

async function change(direction: 'promote' | 'demote'): Promise<void> {
  const target = direction === 'demote' ? demoteTarget.value : promoteTarget.value
  if (!target || busy.value !== null) return
  busy.value = direction
  errorMessage.value = null
  statusMessage.value = ''
  const trimmedReason = reason.value.trim()
  const body = trimmedReason ? { level: target, reason: trimmedReason } : { level: target }
  try {
    const { data, error, response } = await api.POST(
      direction === 'demote'
        ? '/api/v1/pipelines/{pipeline_id}/autonomy/demote'
        : '/api/v1/pipelines/{pipeline_id}/autonomy/promote',
      { params: { path: { pipeline_id: props.pipelineId } }, body },
    )
    if (error) {
      // 403 (not admin/owner) and 422 (direction/ceiling) surface the server's
      // own detail; never leave the UI silently stale.
      errorMessage.value = autonomyErrorMessage(error, response?.status)
      return
    }
    statusMessage.value = t('views.PipelineEditorView.progressive_autonomy_changed', {
      level: levelLabel(data?.earned_autonomy_level ?? target),
    })
    reason.value = ''
    // Let the owning editor refetch the pipeline so the earned display (and
    // the `earned_autonomy_updated_at` stamp) reflects the server response.
    emit('changed')
  } catch (e: unknown) {
    errorMessage.value = t('views.PipelineEditorView.progressive_autonomy_failed', {
      error: formatApiError(e),
    })
  } finally {
    busy.value = null
  }
}

const btnSecondary =
  'inline-flex shrink-0 items-center justify-center whitespace-nowrap rounded-md border border-input bg-background px-2 py-1 text-xs hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50'
</script>
