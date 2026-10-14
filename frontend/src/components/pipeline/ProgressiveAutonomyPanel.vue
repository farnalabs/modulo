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
        <!-- A NULL stored default is the backend's manual_approval default
             (AutonomyLevel.default()), so the row shows that label rather
             than "Unknown level". -->
        <dd class="font-medium text-foreground">{{ levelLabel(defaultLevel ?? 'manual_approval') }}</dd>
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

    <!-- The demote/promote permission rule rides on each button as BOTH a
         title (mouse hover) and an aria-description: a title alone is
         invisible to touch / keyboard assistive tech. -->
    <div class="flex items-center gap-2">
      <button
        type="button"
        :class="btnDemote"
        :disabled="!canDemote || pending !== null"
        :title="$t('views.PipelineEditorView.autonomy_panel_demote_hint')"
        :aria-description="$t('views.PipelineEditorView.autonomy_panel_demote_hint')"
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
        :aria-description="$t('views.PipelineEditorView.autonomy_panel_promote_hint')"
        data-testid="progressive-autonomy-promote"
        @click="apply('promote')"
      >
        {{ pending === 'promote' ? pendingLabel : promoteLabel }}
      </button>
    </div>

    <!-- Async outcome: the native <output> element is an implicit polite live
         region (role=status, aria-live=polite), so an update is announced
         (WCAG 4.1.3) without an explicit status role. -->
    <output
      aria-live="polite"
      :aria-label="$t('views.PipelineEditorView.autonomy_panel_status_label')"
      class="block text-[11px] text-muted-foreground"
      data-testid="progressive-autonomy-status"
    >
      <span v-if="statusMessage">{{ statusMessage }}</span>
    </output>

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
// level immediately, without waiting for the parent's refetch to land. The
// override never outlives fresher prop data: the watch below clears it as
// soon as the parent's refetch either confirms our own write (same value) or
// delivers a different value carrying a timestamp at least as new as the
// override's own (a concurrent writer's fresher write always wins).
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

/**
 * True when the prop's timestamp is at least as new as the override's own.
 * Both are ISO-8601 UTC strings from the API, which sort lexicographically.
 * An unknown prop timestamp cannot prove the prop is stale, so it counts as
 * fresh; an unknown override timestamp cannot defend itself, so anything
 * beats it.
 */
function isPropAtLeastAsNew(propAt: string | null, overrideAt: string | null): boolean {
  if (propAt === null || overrideAt === null) return true
  return propAt >= overrideAt
}

// Drop the local override once the parent's refetch is authoritative: either
// it confirms our own write (same value), or it delivers a DIFFERENT value
// that is at least as new as the override (a concurrent writer's fresher
// write). Only a differing value carrying an OLDER timestamp — a stale
// in-flight response that landed after our write — keeps the override.
watch([() => props.earnedLevel, () => props.earnedUpdatedAt], ([nextLevel, nextAt]) => {
  if (earnedOverride.value === null) return
  const confirmedOwnWrite = nextLevel !== null && nextLevel === earnedOverride.value
  const fresherConcurrentWrite =
    nextLevel !== earnedOverride.value && isPropAtLeastAsNew(nextAt, earnedAtOverride.value)
  if (confirmedOwnWrite || fresherConcurrentWrite) {
    earnedOverride.value = null
    earnedAtOverride.value = null
  }
})

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
