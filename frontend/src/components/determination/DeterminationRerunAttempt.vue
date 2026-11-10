<template>
  <article
    class="rounded-lg border bg-muted/20 p-3 space-y-2"
    data-testid="determination-rerun-attempt"
  >
    <h4 class="text-sm font-medium" data-testid="determination-rerun-attempt-heading">{{ heading }}</h4>
    <dl class="grid gap-1 text-xs">
      <div>
        <dt class="text-muted-foreground">{{ $t('views.DeterminationView.rerun_attempt_id') }}</dt>
        <dd class="font-mono break-all" data-testid="determination-rerun-attempt-id">{{ attemptId }}</dd>
      </div>
      <div>
        <dt class="text-muted-foreground">{{ $t('views.DeterminationView.rerun_attempt_proposal') }}</dt>
        <dd class="font-mono break-all" data-testid="determination-rerun-attempt-proposal">{{ proposalId }}</dd>
      </div>
      <div>
        <dt class="text-muted-foreground">{{ $t('views.DeterminationView.rerun_attempt_status') }}</dt>
        <dd class="font-medium" data-testid="determination-rerun-attempt-status">
          {{ $t('views.DeterminationView.attempt_status_' + attemptStatus) }}
        </dd>
      </div>
      <div>
        <dt class="text-muted-foreground">{{ $t('views.DeterminationView.rerun_attempt_confidence') }}</dt>
        <dd data-testid="determination-rerun-attempt-confidence">
          <span class="rounded-full px-2 py-0.5" :class="confidenceBadgeClass(attemptConfidence)">
            {{ $t('views.DeterminationView.confidence_' + attemptConfidence) }}
          </span>
        </dd>
      </div>
    </dl>

    <div class="space-y-1 text-xs" data-testid="determination-rerun-attempt-uncertainty">
      <p class="font-medium text-muted-foreground">{{ $t('views.DeterminationView.proposal_uncertainty') }}</p>
      <p
        v-if="uncertainty.length === 0"
        class="text-muted-foreground"
        data-testid="determination-rerun-attempt-uncertainty-empty"
      >
        {{ $t('views.DeterminationView.rerun_attempt_uncertainty_empty') }}
      </p>
      <ul v-else class="list-disc space-y-0.5 pl-5 text-muted-foreground">
        <li v-for="(entry, index) in uncertainty" :key="index">{{ entry }}</li>
      </ul>
    </div>

    <div class="space-y-1 text-xs" data-testid="determination-rerun-attempt-unavailable">
      <p class="font-medium text-muted-foreground">{{ $t('views.DeterminationView.unavailable_evidence_title') }}</p>
      <p
        v-if="unavailable.length === 0"
        class="text-muted-foreground"
        data-testid="determination-rerun-attempt-unavailable-empty"
      >
        {{ $t('views.DeterminationView.rerun_attempt_unavailable_empty') }}
      </p>
      <ul v-else class="space-y-1">
        <li
          v-for="entry in unavailable"
          :key="unavailableEvidenceKey(entry)"
          class="rounded border border-destructive/40 bg-destructive/5 p-2"
          data-testid="determination-rerun-attempt-unavailable-row"
        >
          <p class="font-mono break-all">{{ entry.source_id }}</p>
          <p class="text-muted-foreground">
            {{ $t('views.DeterminationView.unavailable_requested_resource') }}: {{ entry.requested_resource }}
          </p>
          <p>
            <span class="font-medium">{{ $t('views.DeterminationView.unavailable_reason') }}:</span>
            <span v-if="reasonFor(entry.reason).withheld" class="italic" data-testid="determination-evidence-withheld">
              {{ $t('views.DeterminationView.evidence_value_withheld') }}
            </span>
            <span v-else>{{ reasonFor(entry.reason).text }}</span>
          </p>
        </li>
      </ul>
    </div>

    <div class="space-y-1 text-xs">
      <p class="font-medium text-muted-foreground">{{ $t('views.DeterminationView.provenance_title') }}</p>
      <DeterminationEvidenceRefs :references="provenance" />
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import DeterminationEvidenceRefs from './DeterminationEvidenceRefs.vue'
import { confidenceBadgeClass } from './confidencePresentation'
import {
  displayEvidenceText,
  unavailableEvidenceKey,
  type DisplayText,
} from './evidencePresentation'
import type { RerunAttemptSummary } from '../../composables/useDetermination'

/**
 * One rerun attempt's identity, confidence, uncertainty and provenance.
 *
 * The two attempts are always rendered with an explicit heading (prior vs
 * new), so the observed side can never be mistaken for the fresh side. The
 * parent renders this component only inside a non-null rerun result, so the
 * attempt is non-null here.
 */
const props = defineProps<{
  heading: string
  attempt: RerunAttemptSummary
}>()

const attemptId = computed(() => props.attempt.attempt_id)
const proposalId = computed(() => props.attempt.proposal_id)
const attemptStatus = computed(() => props.attempt.status)
const attemptConfidence = computed(() => props.attempt.confidence)
const uncertainty = computed(() => props.attempt.uncertainty ?? [])
const unavailable = computed(() => props.attempt.unavailable_evidence ?? [])
const provenance = computed(() => props.attempt.provenance ?? [])

/** A source error reason is free text produced by a connector. */
function reasonFor(reason: string): DisplayText {
  return displayEvidenceText(reason)
}
</script>
