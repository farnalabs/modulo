<template>
  <section class="card p-4 space-y-5" data-testid="determination-review">
    <h2 class="text-lg font-semibold">{{ $t('views.DeterminationView.review_title') }}</h2>

    <section
      v-if="review"
      class="rounded-lg border border-success/40 bg-success/5 p-3 space-y-2"
      aria-live="polite"
      data-testid="determination-review-result"
    >
      <h3 class="text-sm font-semibold">{{ $t('views.DeterminationView.review_result_title') }}</h3>
      <dl class="grid gap-2 text-sm sm:grid-cols-3">
        <div>
          <dt class="text-muted-foreground">{{ $t('views.DeterminationView.review_result_state') }}</dt>
          <dd class="font-medium" data-testid="determination-review-result-state">
            {{ $t('views.DeterminationView.review_state_' + review.review_state) }}
          </dd>
        </div>
        <div>
          <dt class="text-muted-foreground">{{ $t('views.DeterminationView.review_result_actor') }}</dt>
          <dd class="font-medium" data-testid="determination-review-result-actor">{{ review.actor_username }}</dd>
        </div>
        <div>
          <dt class="text-muted-foreground">{{ $t('views.DeterminationView.review_result_decided_at') }}</dt>
          <dd class="font-medium" data-testid="determination-review-result-decided-at">{{ decidedAt }}</dd>
        </div>
      </dl>
      <p v-if="review.reason" class="text-sm" data-testid="determination-review-result-reason">
        <span class="text-muted-foreground">{{ $t('views.DeterminationView.review_result_reason') }}:</span> {{ review.reason }}
      </p>
      <p class="text-sm font-medium" data-testid="determination-review-not-activated">
        {{ $t('views.DeterminationView.review_result_not_activated') }}
      </p>
    </section>

    <div
      v-if="error"
      role="alert"
      aria-live="assertive"
      class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-destructive-text"
      data-testid="determination-review-error"
    >
      <p class="text-sm font-medium">{{ $t('views.DeterminationView.review_failed') }}</p>
      <p class="text-sm">{{ error }}</p>
    </div>

    <section class="space-y-2" data-testid="determination-review-derived">
      <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.review_derived_title') }}</h3>
      <p class="text-sm text-muted-foreground">{{ $t('views.DeterminationView.review_derived_hint') }}</p>
      <p v-if="derivedElements.length === 0" class="text-sm text-muted-foreground" data-testid="determination-review-derived-empty">
        {{ $t('views.DeterminationView.derived_elements_empty') }}
      </p>
      <ul v-else class="space-y-3">
        <li
          v-for="element in derivedElements"
          :key="element.id"
          class="rounded-lg border p-3 space-y-2"
          :class="isRemoved(element.id) ? 'opacity-60 border-dashed' : ''"
          data-testid="determination-review-derived-row"
        >
          <div class="flex flex-wrap items-center justify-between gap-2">
            <span v-if="isRemoved(element.id)" class="text-xs font-medium text-destructive-text">
              {{ $t('views.DeterminationView.review_derived_removed') }}
            </span>
            <button
              type="button"
              class="rounded border px-2 py-1 text-xs"
              :aria-pressed="isRemoved(element.id)"
              :data-testid="`determination-review-remove-${element.id}`"
              @click="toggleRemoved(element.id)"
            >
              {{ isRemoved(element.id) ? $t('views.DeterminationView.review_derived_restore') : $t('views.DeterminationView.review_derived_remove') }}
            </button>
          </div>
          <div class="space-y-1">
            <label class="block text-xs text-muted-foreground" :for="statementId(element.id)">
              {{ $t('views.DeterminationView.review_derived_statement') }}
            </label>
            <input
              :id="statementId(element.id)"
              v-model="drafts[element.id].statement"
              type="text"
              class="w-full rounded border px-2 py-1 text-sm disabled:opacity-60"
              :disabled="isRemoved(element.id)"
              :data-testid="`determination-review-statement-${element.id}`"
            />
          </div>
          <div class="space-y-1">
            <label class="block text-xs text-muted-foreground" :for="confidenceId(element.id)">
              {{ $t('views.DeterminationView.review_derived_confidence') }}
            </label>
            <select
              :id="confidenceId(element.id)"
              v-model="drafts[element.id].confidence"
              class="rounded border px-2 py-1 text-sm disabled:opacity-60"
              :disabled="isRemoved(element.id)"
              :data-testid="`determination-review-confidence-${element.id}`"
            >
              <option v-for="confidence in CONFIDENCE_CHOICES" :key="confidence" :value="confidence">
                {{ $t('views.DeterminationView.confidence_' + confidence) }}
              </option>
            </select>
          </div>
        </li>
      </ul>
      <p
        v-if="hasInvalidStatement"
        class="text-sm text-destructive-text"
        aria-live="polite"
        data-testid="determination-review-statements-invalid"
      >
        {{ $t('views.DeterminationView.review_statements_invalid') }}
      </p>
    </section>

    <section class="space-y-2" data-testid="determination-review-blocking">
      <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.review_acknowledgements_title') }}</h3>
      <p v-if="blockingAssumptions.length === 0" class="text-sm text-muted-foreground" data-testid="determination-review-blocking-empty">
        {{ $t('views.DeterminationView.review_acknowledgements_none') }}
      </p>
      <template v-else>
        <p class="text-sm text-muted-foreground">{{ $t('views.DeterminationView.review_acknowledgements_hint') }}</p>
        <ul class="space-y-2">
          <li v-for="assumption in blockingAssumptions" :key="assumption.id">
            <label class="flex items-start gap-2 rounded-lg border p-3 text-sm">
              <input
                v-model="acknowledgedIds"
                type="checkbox"
                :value="assumption.id"
                class="mt-1"
                :data-testid="`determination-review-acknowledge-${assumption.id}`"
              />
              <span>
                <span class="block font-medium">{{ assumption.statement }}</span>
                <span class="block text-xs text-muted-foreground">
                  {{ $t('views.DeterminationView.review_acknowledgement_label') }} ·
                  {{ $t('views.DeterminationView.confidence_' + assumption.confidence) }}
                </span>
              </span>
            </label>
          </li>
        </ul>
        <p
          v-if="unacknowledgedCount > 0"
          class="text-sm text-warning-text"
          aria-live="polite"
          data-testid="determination-review-approve-blocked"
        >
          {{ $t('views.DeterminationView.review_approve_blocked', { count: unacknowledgedCount }) }}
        </p>
      </template>
    </section>

    <section class="space-y-2">
      <label class="block text-sm font-medium" :for="reasonId">
        {{ $t('views.DeterminationView.review_reason') }}
      </label>
      <textarea
        :id="reasonId"
        v-model="reason"
        rows="3"
        class="w-full rounded border px-3 py-2 text-sm"
        data-testid="determination-review-reason"
      />
      <p class="text-xs text-muted-foreground">{{ $t('views.DeterminationView.review_reason_hint') }}</p>
      <p
        v-if="showReasonRequired"
        class="text-sm text-destructive-text"
        aria-live="polite"
        data-testid="determination-review-reason-required"
      >
        {{ $t('views.DeterminationView.review_reason_required') }}
      </p>
    </section>

    <div class="flex flex-wrap gap-2">
      <button
        type="button"
        class="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
        :disabled="!canApprove"
        data-testid="determination-review-approve"
        @click="submit('approve')"
      >
        {{ loading ? $t('views.DeterminationView.review_recording') : $t('views.DeterminationView.review_approve') }}
      </button>
      <button
        type="button"
        class="rounded-md border border-destructive px-3 py-2 text-sm font-medium text-destructive-text disabled:opacity-50"
        :disabled="!canReject"
        data-testid="determination-review-reject"
        @click="onRejectClick"
      >
        {{ $t('views.DeterminationView.review_reject') }}
      </button>
      <button
        type="button"
        class="rounded-md border px-3 py-2 text-sm font-medium disabled:opacity-50"
        :disabled="!canNeedsReview"
        data-testid="determination-review-needs-review"
        @click="submit('needs_review')"
      >
        {{ $t('views.DeterminationView.review_needs_review') }}
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { formatDateShortWithTime } from '../../lib/formatDate'
import { isUnsettledConfidence } from './confidencePresentation'
import type {
  DeterminationProposal,
  DeterminationReviewResponse,
  ProposalConfidence,
  ReviewCorrections,
  ReviewSubmission,
} from '../../composables/useDetermination'

const CONFIDENCE_CHOICES: ProposalConfidence[] = ['high', 'medium', 'low', 'unknown']
const reasonId = 'determination-review-reason-input'

const props = defineProps<{
  proposal: DeterminationProposal
  review: DeterminationReviewResponse | null
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{
  (event: 'submit', submission: ReviewSubmission): void
  (event: 'corrections-change', corrections: ReviewCorrections): void
}>()

interface ElementDraft {
  statement: string
  confidence: string
}

const drafts = ref<Record<string, ElementDraft>>({})
const removedIds = ref<string[]>([])
const acknowledgedIds = ref<string[]>([])
const reason = ref('')
const showReasonRequired = ref(false)

const derivedElements = computed(() => props.proposal.derived_elements ?? [])

const blockingAssumptions = computed(() =>
  (props.proposal.unresolved_assumptions ?? []).filter((assumption) =>
    isUnsettledConfidence(assumption.confidence),
  ),
)

const unacknowledgedCount = computed(
  () => blockingAssumptions.value.filter((assumption) => !acknowledgedIds.value.includes(assumption.id)).length,
)

const hasInvalidStatement = computed(() =>
  derivedElements.value.some(
    (element) => !isRemoved(element.id) && !(drafts.value[element.id]?.statement ?? '').trim(),
  ),
)

const reasonText = computed(() => reason.value.trim())

const canApprove = computed(
  () => !props.loading && !hasInvalidStatement.value && unacknowledgedCount.value === 0,
)
const canReject = computed(() => !props.loading && !hasInvalidStatement.value)
const canNeedsReview = computed(() => !props.loading && !hasInvalidStatement.value)

const decidedAt = computed(() =>
  props.review ? formatDateShortWithTime(props.review.decided_at) : '',
)

// The panel edits a copy of the proposal: a new draft (a new proposal id)
// starts from the server's own statements and confidences, never from the
// previous draft's edits.
watch(
  () => props.proposal,
  (proposal) => {
    const next: Record<string, ElementDraft> = {}
    for (const element of proposal.derived_elements ?? []) {
      next[element.id] = { statement: element.statement, confidence: element.confidence }
    }
    drafts.value = next
    removedIds.value = []
    acknowledgedIds.value = []
    reason.value = ''
    showReasonRequired.value = false
  },
  { immediate: true },
)

function isRemoved(id: string): boolean {
  return removedIds.value.includes(id)
}

function toggleRemoved(id: string): void {
  removedIds.value = isRemoved(id)
    ? removedIds.value.filter((candidate) => candidate !== id)
    : [...removedIds.value, id]
}

function statementId(id: string): string {
  return `determination-review-statement-input-${id}`
}

function confidenceId(id: string): string {
  return `determination-review-confidence-input-${id}`
}

function correctionEdits(): ReviewSubmission['correctedDerivedElements'] {
  const edits: ReviewSubmission['correctedDerivedElements'] = []
  for (const element of derivedElements.value) {
    if (isRemoved(element.id)) continue
    const draft = drafts.value[element.id]
    if (!draft) continue
    const statement = draft.statement.trim()
    const edit: ReviewSubmission['correctedDerivedElements'][number] = { id: element.id }
    if (statement && statement !== element.statement) edit.statement = statement
    if (draft.confidence !== element.confidence) {
      edit.confidence = draft.confidence as ProposalConfidence
    }
    if (edit.statement !== undefined || edit.confidence !== undefined) edits.push(edit)
  }
  return edits
}

/**
 * The corrections this panel currently holds. Emitted so a rerun can carry
 * them forward by prior element id without the parent reaching into panel
 * state.
 */
const correctionPayload = computed<ReviewCorrections>(() => ({
  correctedDerivedElements: correctionEdits(),
  removedDerivedElementIds: [...removedIds.value],
}))

// Deep equality: an edit that is reverted emits an empty correction set again,
// and an unchanged payload emits nothing.
watch(correctionPayload, (value) => emit('corrections-change', value), { deep: true })

function submit(decision: ReviewSubmission['decision']): void {
  if (props.loading || hasInvalidStatement.value) return
  emit('submit', {
    decision,
    reason: reasonText.value || null,
    correctedDerivedElements: correctionEdits(),
    removedDerivedElementIds: [...removedIds.value],
    acknowledgedAssumptionIds: [...acknowledgedIds.value],
  })
}

function onRejectClick(): void {
  if (reasonText.value.length === 0) {
    showReasonRequired.value = true
    return
  }
  showReasonRequired.value = false
  submit('reject')
}
</script>
