<template>
  <section class="card p-4 space-y-5" data-testid="determination-rerun">
    <header class="space-y-1">
      <h2 class="text-lg font-semibold">{{ $t('views.DeterminationView.rerun_title') }}</h2>
      <p class="text-sm text-muted-foreground">{{ $t('views.DeterminationView.rerun_hint') }}</p>
    </header>

    <p
      class="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm"
      data-testid="determination-rerun-not-activated"
    >
      {{ $t('views.DeterminationView.rerun_not_activated') }}
    </p>

    <p
      v-if="!proposal"
      class="text-sm text-muted-foreground"
      data-testid="determination-rerun-needs-proposal"
    >
      {{ $t('views.DeterminationView.rerun_needs_proposal') }}
    </p>

    <template v-else>
      <p
        v-if="pendingCorrectionCount > 0"
        class="text-sm"
        data-testid="determination-rerun-corrections-pending"
      >
        {{ $t('views.DeterminationView.rerun_corrections_pending', { count: pendingCorrectionCount }) }}
      </p>
      <p
        v-else
        class="text-sm text-muted-foreground"
        data-testid="determination-rerun-corrections-none"
      >
        {{ $t('views.DeterminationView.rerun_corrections_pending_none') }}
      </p>

      <button
        type="button"
        class="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-60"
        :disabled="loading"
        data-testid="determination-rerun-run"
        @click="emit('rerun')"
      >
        {{ loading ? $t('views.DeterminationView.rerun_running') : $t('views.DeterminationView.rerun_run') }}
      </button>

      <div
        v-if="loading"
        class="flex items-center gap-3 text-sm text-muted-foreground"
        aria-live="polite"
        data-testid="determination-rerun-loading"
      >
        <LoadingSpinner />
        <span>{{ $t('views.DeterminationView.rerun_loading') }}</span>
      </div>

      <div
        v-else-if="error"
        role="alert"
        aria-live="assertive"
        class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-destructive-text"
        data-testid="determination-rerun-error"
      >
        <p class="text-sm font-medium">{{ $t('views.DeterminationView.rerun_failed') }}</p>
        <p class="text-sm">{{ error }}</p>
      </div>

      <p
        v-else-if="!result"
        class="text-sm text-muted-foreground"
        data-testid="determination-rerun-idle"
      >
        {{ $t('views.DeterminationView.rerun_idle') }}
      </p>

      <section v-else class="space-y-4" aria-live="polite" data-testid="determination-rerun-result">
        <h3 class="text-base font-semibold">{{ $t('views.DeterminationView.rerun_result_title') }}</h3>

        <section
          v-if="displayIncomplete"
          class="rounded-lg border border-warning/50 bg-warning/10 p-3 space-y-2"
          data-testid="determination-rerun-incomplete"
        >
          <p class="text-sm font-semibold" data-testid="determination-rerun-incomplete-title">
            {{ $t('views.DeterminationView.rerun_incomplete_title') }}
          </p>
          <p class="text-sm" data-testid="determination-rerun-incomplete-hint">
            {{ $t('views.DeterminationView.rerun_incomplete_hint') }}
          </p>
          <ul class="list-disc space-y-1 pl-5 text-sm">
            <li
              v-for="(reason, index) in incompleteReasons"
              :key="index"
              data-testid="determination-rerun-incomplete-reason"
            >
              {{ reason }}
            </li>
          </ul>
        </section>

        <section class="grid gap-3 sm:grid-cols-2" data-testid="determination-rerun-attempts">
          <DeterminationRerunAttempt
            :heading="$t('views.DeterminationView.rerun_prior_attempt')"
            :attempt="priorAttempt"
          />
          <DeterminationRerunAttempt
            :heading="$t('views.DeterminationView.rerun_current_attempt')"
            :attempt="currentAttempt"
          />
        </section>

        <section class="space-y-2" data-testid="determination-rerun-counts">
          <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.rerun_changes_title') }}</h3>
          <ul class="flex flex-wrap gap-2 text-xs">
            <li
              v-for="status in statuses"
              :key="status"
              class="rounded-full px-2 py-0.5"
              :class="elementChangeBadgeClass(status)"
              data-testid="determination-rerun-count"
              :data-status="status"
            >
              {{ statusLabel(status) }}: {{ counts[status] }}
            </li>
          </ul>
        </section>

        <p
          v-if="changes.length === 0"
          class="rounded-lg border border-success/40 bg-success/5 p-3 text-sm"
          data-testid="determination-rerun-no-changes"
        >
          {{ $t('views.DeterminationView.rerun_no_changes') }}
        </p>

        <div v-else class="space-y-3" data-testid="determination-rerun-changes">
          <section
            v-for="status in populatedStatuses"
            :key="status"
            class="rounded-lg border p-3 space-y-2"
            :class="elementChangeGroupClass(status)"
            data-testid="determination-rerun-group"
            :data-status="status"
          >
            <h4 class="text-sm font-medium">{{ statusLabel(status) }} ({{ counts[status] }})</h4>
            <p
              v-if="status === 'not_reproduced'"
              class="text-xs text-muted-foreground"
              data-testid="determination-rerun-group-hint"
            >
              {{ $t('views.DeterminationView.rerun_not_reproduced_hint') }}
            </p>
            <p
              v-else-if="status === 'removed'"
              class="text-xs text-muted-foreground"
              data-testid="determination-rerun-group-hint"
            >
              {{ $t('views.DeterminationView.rerun_removed_hint') }}
            </p>
            <ul class="space-y-2">
              <li
                v-for="change in grouped(status)"
                :key="elementChangeKey(change)"
                class="rounded border bg-muted/20 p-3 space-y-1"
                data-testid="determination-rerun-change"
                :data-status="change.status"
              >
                <p class="text-sm font-medium" data-testid="determination-rerun-change-statement">
                  {{ change.statement }}
                </p>
                <div class="flex flex-wrap items-center gap-2 text-xs">
                  <span
                    v-if="change.prior_confidence"
                    class="rounded-full px-2 py-0.5"
                    :class="confidenceBadgeClass(change.prior_confidence)"
                    data-testid="determination-rerun-change-prior-confidence"
                  >
                    {{ $t('views.DeterminationView.rerun_prior_confidence') }}:
                    {{ $t('views.DeterminationView.confidence_' + change.prior_confidence) }}
                  </span>
                  <span
                    v-if="change.current_confidence"
                    class="rounded-full px-2 py-0.5"
                    :class="confidenceBadgeClass(change.current_confidence)"
                    data-testid="determination-rerun-change-current-confidence"
                  >
                    {{ $t('views.DeterminationView.rerun_current_confidence') }}:
                    {{ $t('views.DeterminationView.confidence_' + change.current_confidence) }}
                  </span>
                </div>
                <div v-if="priorRefs(change).length > 0" class="space-y-1">
                  <p class="text-xs font-medium text-muted-foreground">
                    {{ $t('views.DeterminationView.rerun_prior_evidence') }}
                  </p>
                  <DeterminationEvidenceRefs :references="priorRefs(change)" />
                </div>
                <div v-if="currentRefs(change).length > 0" class="space-y-1">
                  <p class="text-xs font-medium text-muted-foreground">
                    {{ $t('views.DeterminationView.rerun_current_evidence') }}
                  </p>
                  <DeterminationEvidenceRefs :references="currentRefs(change)" />
                </div>
              </li>
            </ul>
          </section>
        </div>

        <section class="space-y-2" data-testid="determination-rerun-inputs">
          <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.rerun_inputs_title') }}</h3>
          <p
            v-if="inputsUnchanged"
            class="rounded-lg border border-success/40 bg-success/5 p-3 text-sm"
            data-testid="determination-rerun-inputs-unchanged"
          >
            {{ $t('views.DeterminationView.rerun_inputs_unchanged') }}
          </p>
          <template v-else>
            <section
              v-if="newSources.length > 0"
              class="space-y-1"
              data-testid="determination-rerun-input-group"
              data-kind="new"
            >
              <h4 class="text-xs font-medium text-muted-foreground">
                {{ $t('views.DeterminationView.rerun_inputs_new') }}
              </h4>
              <ul class="space-y-1 text-sm">
                <li
                  v-for="item in newSources"
                  :key="sourceItemKey(item)"
                  class="rounded-lg border bg-muted/20 p-2 text-xs"
                  data-testid="determination-rerun-input-new"
                >
                  <SourceInventorySummary :item="item" />
                </li>
              </ul>
            </section>

            <section
              v-if="missingSources.length > 0"
              class="space-y-1"
              data-testid="determination-rerun-input-group"
              data-kind="missing"
            >
              <h4 class="text-xs font-medium text-muted-foreground">
                {{ $t('views.DeterminationView.rerun_inputs_missing') }}
              </h4>
              <ul class="space-y-1 text-sm">
                <li
                  v-for="item in missingSources"
                  :key="sourceItemKey(item)"
                  class="rounded-lg border border-destructive/40 bg-destructive/5 p-2 text-xs"
                  data-testid="determination-rerun-input-missing"
                >
                  <SourceInventorySummary :item="item" />
                </li>
              </ul>
            </section>

            <section
              v-if="newlyAvailable.length > 0"
              class="space-y-1"
              data-testid="determination-rerun-input-group"
              data-kind="newly_available"
            >
              <h4 class="text-xs font-medium text-muted-foreground">
                {{ $t('views.DeterminationView.rerun_inputs_newly_available') }}
              </h4>
              <ul class="space-y-1 text-sm">
                <li
                  v-for="sourceId in newlyAvailable"
                  :key="sourceId"
                  class="font-mono text-xs break-all"
                  data-testid="determination-rerun-input-newly-available"
                >
                  {{ sourceId }}
                </li>
              </ul>
            </section>

            <section
              v-if="newlyUnavailable.length > 0"
              class="space-y-1"
              data-testid="determination-rerun-input-group"
              data-kind="newly_unavailable"
            >
              <h4 class="text-xs font-medium text-muted-foreground">
                {{ $t('views.DeterminationView.rerun_inputs_newly_unavailable') }}
              </h4>
              <ul class="space-y-1 text-sm">
                <li
                  v-for="sourceId in newlyUnavailable"
                  :key="sourceId"
                  class="font-mono text-xs break-all"
                  data-testid="determination-rerun-input-newly-unavailable"
                >
                  {{ sourceId }}
                </li>
              </ul>
            </section>

            <section
              v-if="sampleCountChanges.length > 0"
              class="space-y-1"
              data-testid="determination-rerun-input-group"
              data-kind="sample_count"
            >
              <h4 class="text-xs font-medium text-muted-foreground">
                {{ $t('views.DeterminationView.rerun_inputs_sample_counts') }}
              </h4>
              <ul class="space-y-1 text-sm">
                <li
                  v-for="change in sampleCountChanges"
                  :key="sampleCountChangeKey(change)"
                  class="text-xs"
                  data-testid="determination-rerun-input-sample-count"
                >
                  {{
                    $t('views.DeterminationView.rerun_sample_count_change', {
                      source: change.source_id,
                      prior: change.prior_sample_count,
                      current: change.current_sample_count,
                    })
                  }}
                </li>
              </ul>
            </section>
          </template>
        </section>

        <section class="space-y-2" data-testid="determination-rerun-corrections">
          <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.rerun_corrections_title') }}</h3>
          <p
            v-if="correctionsEmpty"
            class="text-sm text-muted-foreground"
            data-testid="determination-rerun-corrections-none-result"
          >
            {{ $t('views.DeterminationView.rerun_corrections_none') }}
          </p>
          <template v-else>
            <div v-if="hasMatched" class="space-y-1" data-testid="determination-rerun-corrections-matched">
              <h4 class="text-xs font-medium text-muted-foreground">
                {{ $t('views.DeterminationView.rerun_corrections_matched') }}
              </h4>
              <ul class="flex flex-wrap gap-2 text-xs">
                <li
                  v-for="id in matchedIds"
                  :key="id.key"
                  class="rounded border bg-muted/20 px-2 py-1 font-mono break-all"
                  data-testid="determination-rerun-correction-id"
                  :data-kind="id.kind"
                  :data-matched="'true'"
                >
                  {{ id.value }}
                </li>
              </ul>
            </div>
            <div v-if="hasUnmatched" class="space-y-1" data-testid="determination-rerun-corrections-unmatched">
              <h4 class="text-xs font-medium text-muted-foreground">
                {{ $t('views.DeterminationView.rerun_corrections_unmatched') }}
              </h4>
              <p class="text-xs text-muted-foreground" data-testid="determination-rerun-corrections-unmatched-hint">
                {{ $t('views.DeterminationView.rerun_corrections_unmatched_hint') }}
              </p>
              <ul class="flex flex-wrap gap-2 text-xs">
                <li
                  v-for="id in unmatchedIds"
                  :key="id.key"
                  class="rounded border border-warning/40 bg-warning/10 px-2 py-1 font-mono break-all"
                  data-testid="determination-rerun-correction-id"
                  :data-kind="id.kind"
                  :data-matched="'false'"
                >
                  {{ id.value }}
                </li>
              </ul>
            </div>
          </template>
        </section>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import LoadingSpinner from '../shared/LoadingSpinner.vue'
import DeterminationEvidenceRefs from './DeterminationEvidenceRefs.vue'
import DeterminationRerunAttempt from './DeterminationRerunAttempt.vue'
import SourceInventorySummary from './DeterminationRerunSourceSummary.vue'
import { confidenceBadgeClass } from './confidencePresentation'
import {
  ELEMENT_CHANGE_STATUSES,
  changesByStatus,
  correctionsAreEmpty,
  elementChangeBadgeClass,
  elementChangeCounts,
  elementChangeGroupClass,
  elementChangeKey,
  rerunReportsIncomplete,
  sampleCountChangeKey,
  sourceItemKey,
  type ElementChange,
  type ElementChangeStatus,
} from './rerunPresentation'
import type {
  DeterminationProposal,
  DeterminationRerunResult,
  ReviewCorrections,
} from '../../composables/useDetermination'

/**
 * Rerun determination against the proposal on screen (FAR-2474).
 *
 * The panel is display-only: the view/composable owns the request. It renders
 * exactly what the rerun result reports - both attempt identities, the
 * per-inference classification, the source-inventory changes and the
 * corrections that were (or were not) carried forward - and it never presents
 * a partial rerun as authoritative or conflates "not reproduced" with
 * "removed".
 */
const props = defineProps<{
  proposal: DeterminationProposal | null
  corrections: ReviewCorrections
  result: DeterminationRerunResult | null
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{ (event: 'rerun'): void }>()

const { t } = useI18n()

const statuses = ELEMENT_CHANGE_STATUSES

const pendingCorrectionCount = computed(
  () =>
    props.corrections.correctedDerivedElements.length + props.corrections.removedDerivedElementIds.length,
)

const changes = computed<ElementChange[]>(() => props.result!.element_changes ?? [])
const counts = computed(() => elementChangeCounts(changes.value))
const populatedStatuses = computed(() => statuses.filter((status) => counts.value[status] > 0))

const displayIncomplete = computed(() => rerunReportsIncomplete(props.result!))

const incompleteReasons = computed<string[]>(() => {
  const result = props.result!
  const reasons = [...(result.incomplete_reasons ?? [])]
  for (const sourceId of result.source_changes.newly_unavailable ?? []) {
    reasons.push(t('views.DeterminationView.rerun_newly_unavailable_reason', { source: sourceId }))
  }
  for (const item of result.source_changes.missing_sources ?? []) {
    // The backend already names every missing source in its own reasons; only
    // add one when a deployment's reasons did not mention this source.
    if (!reasons.some((reason) => reason.includes(item.source_id))) {
      reasons.push(t('views.DeterminationView.rerun_missing_source_reason', { source: item.source_id }))
    }
  }
  if (reasons.length === 0 && displayIncomplete.value) {
    reasons.push(t('views.DeterminationView.rerun_gaps_reason'))
  }
  return reasons
})

const priorAttempt = computed(() => props.result!.prior_attempt)
const currentAttempt = computed(() => props.result!.current_attempt)

const inputsUnchanged = computed(() => props.result!.input_set_unchanged)
const newSources = computed(() => props.result!.source_changes.new_sources ?? [])
const missingSources = computed(() => props.result!.source_changes.missing_sources ?? [])
const newlyAvailable = computed(() => props.result!.source_changes.newly_available ?? [])
const newlyUnavailable = computed(() => props.result!.source_changes.newly_unavailable ?? [])
const sampleCountChanges = computed(() => props.result!.source_changes.sample_count_changes ?? [])

const matchedIds = computed(() => {
  const corrections = props.result!.corrections
  return [
    ...(corrections.corrected_derived_element_ids ?? []).map((value) => ({
      key: `corrected:${value}`,
      kind: 'corrected',
      value,
    })),
    ...(corrections.removed_derived_element_ids ?? []).map((value) => ({
      key: `removed:${value}`,
      kind: 'removed',
      value,
    })),
  ]
})

const unmatchedIds = computed(() => {
  const corrections = props.result!.corrections
  return [
    ...(corrections.unmatched_corrected_derived_element_ids ?? []).map((value) => ({
      key: `corrected:${value}`,
      kind: 'corrected',
      value,
    })),
    ...(corrections.unmatched_removed_derived_element_ids ?? []).map((value) => ({
      key: `removed:${value}`,
      kind: 'removed',
      value,
    })),
  ]
})

const hasMatched = computed(() => matchedIds.value.length > 0)
const hasUnmatched = computed(() => unmatchedIds.value.length > 0)
const correctionsEmpty = computed(() => correctionsAreEmpty(props.result!.corrections))

function statusLabel(status: ElementChangeStatus): string {
  return t(`views.DeterminationView.rerun_status_${status}`)
}

function grouped(status: ElementChangeStatus): ElementChange[] {
  return changesByStatus(changes.value, status)
}

function priorRefs(change: ElementChange) {
  return change.prior_evidence_refs ?? []
}

function currentRefs(change: ElementChange) {
  return change.current_evidence_refs ?? []
}
</script>
