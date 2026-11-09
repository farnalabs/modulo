<template>
  <section class="space-y-4" data-testid="determination-provenance-trail">
    <header class="space-y-1">
      <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.provenance_trail_title') }}</h3>
      <p class="text-xs text-muted-foreground">{{ $t('views.DeterminationView.provenance_trail_hint') }}</p>
    </header>

    <dl class="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
      <div>
        <dt class="text-muted-foreground">{{ $t('views.DeterminationView.provenance_attempt_id') }}</dt>
        <dd class="font-mono text-xs break-all" data-testid="determination-provenance-attempt-id">
          {{ proposal.attempt.id }}
        </dd>
      </div>
      <div>
        <dt class="text-muted-foreground">{{ $t('views.DeterminationView.provenance_attempt_status') }}</dt>
        <dd class="font-medium" data-testid="determination-provenance-attempt-status">
          {{ $t('views.DeterminationView.attempt_status_' + proposal.attempt.status) }}
        </dd>
      </div>
      <div>
        <dt class="text-muted-foreground">{{ $t('views.DeterminationView.provenance_contract_version') }}</dt>
        <dd class="font-mono text-xs break-all" data-testid="determination-provenance-contract-version">
          {{ proposal.contract_version }}
        </dd>
      </div>
      <div>
        <dt class="text-muted-foreground">{{ $t('views.DeterminationView.proposal_confidence') }}</dt>
        <dd data-testid="determination-proposal-confidence">
          <span class="rounded-full px-2 py-0.5 text-xs" :class="confidenceBadgeClass(proposal.confidence)">
            {{ $t('views.DeterminationView.confidence_' + proposal.confidence) }}
          </span>
        </dd>
      </div>
    </dl>

    <section class="space-y-2" data-testid="determination-proposal-uncertainty">
      <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.proposal_uncertainty') }}</h3>
      <p v-if="uncertainty.length === 0" class="text-sm text-muted-foreground">
        {{ $t('views.DeterminationView.proposal_uncertainty_empty') }}
      </p>
      <ul v-else class="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
        <li v-for="entry in uncertainty" :key="entry">{{ entry }}</li>
      </ul>
    </section>

    <section class="space-y-2" data-testid="determination-provenance-decision">
      <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.provenance_decision_title') }}</h3>
      <p
        v-if="!review"
        class="text-sm text-muted-foreground"
        data-testid="determination-provenance-decision-empty"
      >
        {{ $t('views.DeterminationView.provenance_decision_none') }}
      </p>
      <dl v-else class="grid gap-3 text-sm sm:grid-cols-3" aria-live="polite">
        <div>
          <dt class="text-muted-foreground">{{ $t('views.DeterminationView.provenance_decision_state') }}</dt>
          <dd class="font-medium" data-testid="determination-provenance-decision-state">
            {{ $t('views.DeterminationView.decision_record_' + review.decision) }}
          </dd>
        </div>
        <div>
          <dt class="text-muted-foreground">{{ $t('views.DeterminationView.provenance_decision_actor') }}</dt>
          <dd class="font-medium" data-testid="determination-provenance-decision-actor">
            {{ review.actor_username }}
          </dd>
        </div>
        <div>
          <dt class="text-muted-foreground">{{ $t('views.DeterminationView.provenance_decision_decided_at') }}</dt>
          <dd class="font-medium" data-testid="determination-provenance-decision-decided-at">
            {{ formatDecidedAt(review) }}
          </dd>
        </div>
      </dl>
    </section>

    <section class="space-y-2" data-testid="determination-provenance">
      <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.provenance_title') }}</h3>
      <p v-if="provenance.length === 0" class="text-sm text-muted-foreground">
        {{ $t('views.DeterminationView.provenance_empty') }}
      </p>
      <ul v-else class="space-y-1 text-sm">
        <li
          v-for="reference in provenance"
          :key="reference.reference_id"
          class="rounded-lg border bg-muted/20 p-3 text-xs"
          data-testid="determination-provenance-row"
        >
          <span class="font-mono break-all">{{ reference.reference_id }}</span>
          <span class="mx-1">·</span>
          <span class="font-mono break-all">{{ reference.source_id }}</span>
          <span class="mx-1">·</span>
          <span v-if="locatorFor(reference).withheld" class="italic" data-testid="determination-evidence-withheld">
            {{ $t('views.DeterminationView.evidence_value_withheld') }}
          </span>
          <span v-else class="font-mono break-all">{{ locatorFor(reference).text }}</span>
          <span class="mx-1">·</span>
          <span
            class="rounded-full px-2 py-0.5"
            :class="provenanceBadgeClass(reference.provenance)"
            data-testid="determination-provenance-row-provenance"
          >
            {{ $t('views.DeterminationView.provenance_' + reference.provenance) }}
          </span>
        </li>
      </ul>
    </section>

    <section class="space-y-2" data-testid="determination-source-inventory">
      <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.source_inventory_title') }}</h3>
      <p v-if="sources.length === 0" class="text-sm text-muted-foreground">
        {{ $t('views.DeterminationView.source_inventory_empty') }}
      </p>
      <ul v-else class="space-y-1 text-sm">
        <li
          v-for="source in sources"
          :key="source.source_id"
          :id="sourceRowId(source.source_id)"
          class="space-y-1 rounded-lg border bg-muted/20 p-3"
          :class="{ 'ring-2 ring-primary': highlightedSourceId === source.source_id }"
          data-testid="determination-source-row"
        >
          <div class="flex flex-wrap items-baseline gap-2">
            <span class="font-mono text-xs break-all">{{ source.source_id }}</span>
            <span class="rounded-full px-2 py-0.5 text-xs" :class="source.status === 'available' ? 'bg-success/10 text-success-text' : 'bg-destructive/10 text-destructive-text'">
              {{ $t('views.DeterminationView.source_status_' + source.status) }}
            </span>
          </div>
          <p class="text-xs text-muted-foreground">
            {{ $t('views.DeterminationView.source_connector_type') }}: {{ source.connector_type }}
            <span class="mx-1">·</span>
            {{ $t('views.DeterminationView.source_resource') }}: {{ source.resource }}
            <span class="mx-1">·</span>
            {{ $t('views.DeterminationView.source_sample_count') }}: {{ source.sample_count }}
          </p>
          <p v-if="source.error" class="text-xs text-destructive-text" data-testid="determination-source-error">
            {{ $t('views.DeterminationView.source_error') }}:
            <span v-if="sourceErrorFor(source.error).withheld" class="italic">
              {{ $t('views.DeterminationView.evidence_value_withheld') }}
            </span>
            <span v-else>{{ sourceErrorFor(source.error).text }}</span>
          </p>
          <p
            v-if="referenceCountsBySource[source.source_id]"
            class="text-xs text-muted-foreground"
            data-testid="determination-source-ref-count"
          >
            {{ $t('views.DeterminationView.source_ref_count', referenceCountsBySource[source.source_id]) }}
          </p>
        </li>
      </ul>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { formatDateShortWithTime } from '../../lib/formatDate'
import { confidenceBadgeClass } from './confidencePresentation'
import {
  displayEvidenceText,
  provenanceBadgeClass,
  type DisplayText,
  type EvidenceReference,
} from './evidencePresentation'
import type {
  DeterminationProposal,
  DeterminationReviewResponse,
} from '../../composables/useDetermination'

const props = defineProps<{
  proposal: DeterminationProposal
  review: DeterminationReviewResponse | null
  highlightedSourceId?: string | null
}>()

const uncertainty = computed(() => props.proposal.uncertainty ?? [])
const provenance = computed(() => props.proposal.provenance ?? [])
const sources = computed(() => props.proposal.attempt.source_inventory ?? [])

/**
 * How many element-level evidence references cite each sampled source, so a
 * source row cross-links back to the elements that depend on it.
 */
const referenceCountsBySource = computed<Record<string, number>>(() => {
  const counts: Record<string, number> = {}
  const elements = [
    ...(props.proposal.observed_facts ?? []),
    ...(props.proposal.derived_elements ?? []),
    ...(props.proposal.unresolved_assumptions ?? []),
  ]
  for (const element of elements) {
    for (const reference of element.evidence_refs ?? []) {
      counts[reference.source_id] = (counts[reference.source_id] ?? 0) + 1
    }
  }
  return counts
})

function sourceRowId(sourceId: string): string {
  return `determination-source-${sourceId.replace(/[^a-zA-Z0-9_-]/g, '-')}`
}

function formatDecidedAt(review: DeterminationReviewResponse): string {
  return formatDateShortWithTime(review.decided_at)
}

/** Locators and connector errors are free text and can carry a credential. */
function locatorFor(reference: EvidenceReference): DisplayText {
  return displayEvidenceText(reference.locator)
}

function sourceErrorFor(error: string): DisplayText {
  return displayEvidenceText(error)
}
</script>
