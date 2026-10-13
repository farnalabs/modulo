<template>
  <!-- FAR-1696 (ADR 043 slice S5): progressive-autonomy visibility surface.
       Read-only display of the pipeline's three autonomy levels (default,
       ceiling, earned) plus the admin/owner manual demote / promote controls
       wired to the live POST /autonomy/{demote,promote} endpoints. The panel
       never mutates the ceiling control — that stays the editor's own select. -->
  <section
    class="space-y-2"
    data-testid="progressive-autonomy-panel"
    :aria-label="$t('views.PipelineEditorView.autonomy_panel_title')"
  >
    <div>
      <h3 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {{ $t('views.PipelineEditorView.autonomy_panel_title') }}
      </h3>
      <p class="mt-1 text-[11px] text-muted-foreground">
        {{ $t('views.PipelineEditorView.autonomy_panel_caption') }}
      </p>
    </div>

    <dl class="space-y-1 text-xs">
      <div
        class="flex items-baseline justify-between gap-2"
        data-testid="progressive-autonomy-default"
      >
        <dt class="text-muted-foreground">
          {{ $t('views.PipelineEditorView.autonomy_panel_default') }}
        </dt>
        <dd class="font-medium text-foreground">{{ levelLabel(defaultLevel) }}</dd>
      </div>
      <div
        class="flex items-baseline justify-between gap-2"
        data-testid="progressive-autonomy-ceiling"
      >
        <dt class="text-muted-foreground">
          {{ $t('views.PipelineEditorView.autonomy_panel_ceiling') }}
        </dt>
        <dd class="font-medium text-foreground">{{ ceilingLabel }}</dd>
      </div>
      <!-- A NULL earned level is the normal pre-promote state, never a blank
           cell: it renders the explicit "using the pipeline default" fallback. -->
      <div
        class="flex items-baseline justify-between gap-2"
        data-testid="progressive-autonomy-earned"
      >
        <dt class="text-muted-foreground">
          {{ $t('views.PipelineEditorView.autonomy_panel_earned') }}
        </dt>
        <dd class="font-medium text-foreground">
          <template v-if="displayEarned">{{ levelLabel(displayEarned) }}</template>
          <template v-else>{{ $t('views.PipelineEditorView.autonomy_panel_earned_none') }}</template>
        </dd>
      </div>
    </dl>

    <p
      v-if="displayEarnedAt"
      class="text-[11px] text-muted-foreground"
      data-testid="progressive-autonomy-earned-at"
    >
      {{ $t('views.PipelineEditorView.autonomy_panel_earned_at', { when: formattedEarnedAt }) }}
    </p>

    <div class="flex items-center gap-2">
      <label
        for="progressive-autonomy-target"
        class="whitespace-nowrap text-[11px] text-muted-foreground"
      >
        {{ $t('views.PipelineEditorView.autonomy_panel_target') }}
      </label>
      <select
        id="progressive-autonomy-target"
        :value="targetLevel"
        class="h-7 flex-1 rounded-md border border-input bg-background px-1.5 text-xs"
        data-testid="progressive-autonomy-target"
        @change="onTargetChange"
      >
        <option
          v-for="level in LEVELS"
          :key="level"
          :value="level"
          :disabled="isAboveCeiling(level)"
        >
          {{ levelLabel(level) }}
        </option>
      </select>
    </div>

    <div>
      <label
        for="progressive-autonomy-reason"
        class="mb-1 block text-[11px] text-muted-foreground"
      >
        {{ $t('views.PipelineEditorView.autonomy_panel_reason') }}
      </label>
      <input
        id="progressive-autonomy-reason"
        v-model="reason"
        type="text"
        maxlength="2000"
        class="h-7 w-full rounded-md border border-input bg-background px-1.5 text-xs"
        data-testid="progressive-autonomy-reason"
      />
    </div>

    <div class="flex items-center gap-2">
      <button
        type="button"
        :class="btnDemote"
        :disabled="!canDemote || pending !== null"
        :title="$t('views.PipelineEditorView.autonomy_panel_demote_hint')"
        data-testid="progressive-autonomy-demote"
        @click="apply('demote')"
      >
        {{ pending === 'demote' ? pendingLabel : demoteLabel }}
      </button>
      <button
        type="button"
        :class="btnPromote"
        :disabled="!canPromote || pending !== null"
        :title="$t('views.PipelineEditorView.autonomy_panel_promote_hint')"
        data-testid="progressive-autonomy-promote"
        @click="apply('promote')"
      >
        {{ pending === 'promote' ? pendingLabel : promoteLabel }}
      </button>
    </div>

    <!-- Async outcome: a polite live region that stays mounted so an update is
         announced (WCAG 4.1.3). -->
    <div
      role="status"
      aria-live="polite"
      class="text-[11px] text-muted-foreground"
      data-testid="progressive-autonomy-status"
    >
      <span v-if="statusMessage">{{ statusMessage }}</span>
    </div>

    <!-- Refusals (403 not-admin/not-owner) and 422 direction/ceiling
         violations surface the server's own detail: never a silent failure. -->
    <div
      v-if="errorMessage"
      role="alert"
      aria-live="assertive"
      class="text-[11px] text-destructive"
      data-testid="progressive-autonomy-error"
    >
      {{ errorMessage }}
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../../lib/api/client'
import { formatApiError, isProblemDetail } from '../../lib/api/formatError'
import { formatDateShortWithTime } from '../../lib/formatDate'

/** The autonomy ladder, lowest first (same canonical values the API uses). */
const LEVELS = ['manual_approval', 'notify_on_complete', 'fully_autonomous'] as const

const RANK: Record<string, number> = {
  manual_approval: 0,
  notify_on_complete: 1,
  fully_autonomous: 2,
}

const props = defineProps<{
  pipelineId: string
  /** Pipeline default_autonomy_level (the level a NULL earned falls back to). */
  defaultLevel: string | null
  /** max_autonomy_level ceiling; NULL means the ceiling is the default level. */
  maxLevel: string | null
  /** Runtime earned level; NULL until something has promoted/demoted it. */
  earnedLevel: string | null
  earnedUpdatedAt: string | null
}>()

const emit = defineEmits<{ (e: 'changed'): void }>()

const { t } = useI18n()

const reason = ref('')
const pending = ref<'demote' | 'promote' | null>(null)
const statusMessage = ref<string | null>(null)
const errorMessage = ref<string | null>(null)

// A successful change updates these locally so the panel reflects the new
// level immediately, without waiting for the parent's refetch to land; a
// parent refetch that delivers the same value clears them (see the watch
// below) so the panel never keeps an override over fresher prop data.
const earnedOverride = ref<string | null>(null)
const earnedAtOverride = ref<string | null>(null)

function rank(level: string | null | undefined): number {
  if (level && level in RANK) return RANK[level]
  return -1
}

const displayEarned = computed(() => earnedOverride.value ?? props.earnedLevel)
const displayEarnedAt = computed(() => earnedAtOverride.value ?? props.earnedUpdatedAt)
const formattedEarnedAt = computed(() =>
  displayEarnedAt.value ? formatDateShortWithTime(displayEarnedAt.value) : '',
)

/** The level a demote/promote moves FROM: earned, else the default. */
const currentLevel = computed(
  () => displayEarned.value || props.defaultLevel || 'manual_approval',
)

/** NULL ceiling means the effective ceiling is the default level. */
const ceilingLevel = computed(() => props.maxLevel || props.defaultLevel || 'manual_approval')

const targetLevel = ref<string>(currentLevel.value)

const targetRank = computed(() => rank(targetLevel.value))
const currentRank = computed(() => rank(currentLevel.value))
const ceilingRank = computed(() => rank(ceilingLevel.value))

function isAboveCeiling(level: string): boolean {
  return rank(level) > ceilingRank.value
}

// Direction guards: demote must strictly lower, promote must strictly raise
// AND stay within the ceiling — a target above the ceiling is not selectable
// (the <option> is disabled) and never enables the promote button.
const canDemote = computed(() => targetRank.value < currentRank.value)
const canPromote = computed(
  () => targetRank.value > currentRank.value && targetRank.value <= ceilingRank.value,
)

const demoteLabel = computed(() => t('views.PipelineEditorView.autonomy_panel_demote'))
const promoteLabel = computed(() => t('views.PipelineEditorView.autonomy_panel_promote'))
const pendingLabel = computed(() => t('views.PipelineEditorView.autonomy_panel_applying'))

const ceilingLabel = computed(() =>
  props.maxLevel
    ? levelLabel(props.maxLevel)
    : t('views.PipelineEditorView.autonomy_panel_ceiling_inherited'),
)

const btnDemote =
  'inline-flex h-7 shrink-0 items-center justify-center whitespace-nowrap rounded-md border border-destructive/50 bg-destructive/10 px-2 text-xs font-medium text-destructive transition-colors hover:bg-destructive/20 disabled:cursor-not-allowed disabled:opacity-50'
const btnPromote =
  'inline-flex h-7 shrink-0 items-center justify-center whitespace-nowrap rounded-md border border-input bg-primary px-2 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50'

function levelLabel(level: string | null): string {
  if (level === 'manual_approval') return t('views.PipelineEditorView.autonomy_manual_approval')
  if (level === 'notify_on_complete') {
    return t('views.PipelineEditorView.autonomy_notify_on_complete')
  }
  if (level === 'fully_autonomous') {
    return t('views.PipelineEditorView.autonomy_fully_autonomous')
  }
  return level ?? t('views.PipelineEditorView.autonomy_panel_unknown')
}

function onTargetChange(event: Event): void {
  targetLevel.value = (event.target as HTMLSelectElement).value
}

/**
 * Keep the selected target usable when the levels move underneath it (the
 * parent refetches after a change): never leave a selection that equals the
 * current level or sits above the ceiling.
 */
watch([currentLevel, ceilingLevel], ([cur]) => {
  if (rank(targetLevel.value) > ceilingRank.value || rank(targetLevel.value) === rank(cur)) {
    targetLevel.value = cur
  }
})

watch(
  () => props.earnedLevel,
  (next) => {
    if (next !== null && next === earnedOverride.value) earnedOverride.value = null
  },
)
watch(
  () => props.earnedUpdatedAt,
  (next) => {
    if (next !== null && next === earnedAtOverride.value) earnedAtOverride.value = null
  },
)

/**
 * POST a manual autonomy change. 403 (not an admin / not on the owner team)
 * gets its own specific message; a 422 (direction or ceiling violation, or an
 * unknown level) shows the server's detail verbatim. Every failure lands in
 * the alert region — the UI is never left silently stale.
 */
async function apply(direction: 'demote' | 'promote'): Promise<void> {
  if (pending.value) return
  const level = targetLevel.value
  const trimmed = reason.value.trim()
  pending.value = direction
  statusMessage.value = null
  errorMessage.value = null
  try {
    const { data, error, response } = await api.POST(
      direction === 'demote'
        ? '/api/v1/pipelines/{pipeline_id}/autonomy/demote'
        : '/api/v1/pipelines/{pipeline_id}/autonomy/promote',
      {
        params: { path: { pipeline_id: props.pipelineId } },
        body: trimmed ? { level, reason: trimmed } : { level },
      },
    )
    if (error) {
      const status =
        response?.status ?? (isProblemDetail(error as unknown) ? (error as { status: number }).status : 0)
      errorMessage.value =
        status === 403
          ? t('views.PipelineEditorView.autonomy_panel_forbidden', {
              error: formatApiError(error),
            })
          : t('views.PipelineEditorView.autonomy_panel_failed', {
              error: formatApiError(error),
            })
      return
    }
    const nextLevel = data?.earned_autonomy_level ?? level
    earnedOverride.value = nextLevel
    earnedAtOverride.value = data?.updated_at ?? null
    statusMessage.value = t(
      direction === 'demote'
        ? 'views.PipelineEditorView.autonomy_panel_demoted'
        : 'views.PipelineEditorView.autonomy_panel_promoted',
      { level: levelLabel(nextLevel) },
    )
    reason.value = ''
    emit('changed')
  } catch (e: unknown) {
    errorMessage.value = t('views.PipelineEditorView.autonomy_panel_failed', {
      error: formatApiError(e),
    })
  } finally {
    pending.value = null
  }
}
</script>
