<template>
  <section class="card p-4 space-y-5" data-testid="determination-proposal">
    <header class="flex flex-wrap items-baseline justify-between gap-2">
      <h2 class="text-lg font-semibold">{{ $t('views.DeterminationView.proposal_title') }}</h2>
      <span v-if="proposal" class="text-xs text-muted-foreground font-mono">
        {{ proposal.contract_version }}
      </span>
    </header>

    <div
      v-if="loading"
      class="flex items-center gap-3 text-sm text-muted-foreground"
      aria-live="polite"
      data-testid="determination-proposal-loading"
    >
      <LoadingSpinner />
      <span>{{ $t('views.DeterminationView.proposal_loading') }}</span>
    </div>

    <ErrorAlert v-else-if="error" :message="error" :on-retry="onRetry" />

    <p v-else-if="!proposal" class="text-sm text-muted-foreground" data-testid="determination-proposal-empty">
      {{ $t('views.DeterminationView.proposal_empty') }}
    </p>

    <template v-else>
      <p class="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm" data-testid="determination-proposal-not-activated">
        {{ $t('views.DeterminationView.proposal_not_activated') }}
      </p>

      <dl class="grid gap-3 text-sm sm:grid-cols-3">
        <div>
          <dt class="text-muted-foreground">{{ $t('views.DeterminationView.proposal_attempt_status') }}</dt>
          <dd class="font-medium">{{ proposal.attempt.status }}</dd>
        </div>
        <div>
          <dt class="text-muted-foreground">{{ $t('views.DeterminationView.proposal_confidence') }}</dt>
          <dd class="font-medium" data-testid="determination-proposal-confidence">
            <span class="rounded-full px-2 py-0.5 text-xs" :class="confidenceBadgeClass(proposal.confidence)">
              {{ $t('views.DeterminationView.confidence_' + proposal.confidence) }}
            </span>
          </dd>
        </div>
        <div>
          <dt class="text-muted-foreground">{{ $t('views.DeterminationView.proposal_review_state') }}</dt>
          <dd class="font-medium">{{ $t('views.DeterminationView.review_state_' + proposal.review_state) }}</dd>
        </div>
      </dl>

      <section class="space-y-2" data-testid="determination-proposal-uncertainty">
        <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.proposal_uncertainty') }}</h3>
        <p v-if="uncertainty.length === 0" class="text-sm text-muted-foreground">
          {{ $t('views.DeterminationView.proposal_uncertainty_empty') }}
        </p>
        <ul v-else class="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
          <li v-for="(entry, index) in uncertainty" :key="index">{{ entry }}</li>
        </ul>
      </section>

      <section class="space-y-2" data-testid="determination-observed-facts">
        <h3 class="text-sm font-medium">
          {{ $t('views.DeterminationView.observed_facts_title') }}
          <span class="text-muted-foreground">({{ observedFacts.length }})</span>
        </h3>
        <p v-if="observedFacts.length === 0" class="text-sm text-muted-foreground">
          {{ $t('views.DeterminationView.observed_facts_empty') }}
        </p>
        <ul v-else class="space-y-2">
          <li
            v-for="fact in observedFacts"
            :key="fact.id"
            class="rounded-lg border bg-muted/20 p-3"
            data-testid="determination-observed-fact"
          >
            <div class="flex flex-wrap items-center justify-between gap-2">
              <p class="text-sm font-medium">{{ fact.statement }}</p>
              <span class="rounded-full px-2 py-0.5 text-xs" :class="confidenceBadgeClass(fact.confidence)">
                {{ $t('views.DeterminationView.confidence_' + fact.confidence) }}
              </span>
            </div>
            <DeterminationEvidenceRefs :references="fact.evidence_refs ?? []" />
          </li>
        </ul>
      </section>

      <section class="space-y-2" data-testid="determination-derived-elements">
        <h3 class="text-sm font-medium">
          {{ $t('views.DeterminationView.derived_elements_title') }}
          <span class="text-muted-foreground">({{ derivedElements.length }})</span>
        </h3>
        <p v-if="derivedElements.length === 0" class="text-sm text-muted-foreground">
          {{ $t('views.DeterminationView.derived_elements_empty') }}
        </p>
        <ul v-else class="space-y-2">
          <li
            v-for="element in derivedElements"
            :key="element.id"
            class="rounded-lg border bg-muted/20 p-3"
            data-testid="determination-derived-element"
          >
            <div class="flex flex-wrap items-center justify-between gap-2">
              <p class="text-sm font-medium">{{ element.statement }}</p>
              <span class="rounded-full px-2 py-0.5 text-xs" :class="confidenceBadgeClass(element.confidence)">
                {{ $t('views.DeterminationView.confidence_' + element.confidence) }}
              </span>
            </div>
            <DeterminationEvidenceRefs :references="element.evidence_refs ?? []" />
          </li>
        </ul>
      </section>

      <section class="space-y-2" data-testid="determination-assumptions">
        <h3 class="text-sm font-medium">
          {{ $t('views.DeterminationView.assumptions_title') }}
          <span class="text-muted-foreground">({{ assumptions.length }})</span>
        </h3>
        <p v-if="assumptions.length === 0" class="text-sm text-muted-foreground">
          {{ $t('views.DeterminationView.assumptions_empty') }}
        </p>
        <ul v-else class="space-y-2">
          <li
            v-for="assumption in assumptions"
            :key="assumption.id"
            class="rounded-lg border bg-muted/20 p-3"
            data-testid="determination-assumption"
          >
            <div class="flex flex-wrap items-center justify-between gap-2">
              <p class="text-sm font-medium">{{ assumption.statement }}</p>
              <span class="rounded-full px-2 py-0.5 text-xs" :class="confidenceBadgeClass(assumption.confidence)">
                {{ $t('views.DeterminationView.confidence_' + assumption.confidence) }}
              </span>
            </div>
            <p class="mt-1 text-xs text-muted-foreground">
              <span class="font-medium">{{ $t('views.DeterminationView.assumption_reason') }}:</span> {{ assumption.reason }}
            </p>
            <DeterminationEvidenceRefs :references="assumption.evidence_refs ?? []" />
          </li>
        </ul>
      </section>

      <section class="space-y-2" data-testid="determination-unavailable-evidence">
        <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.unavailable_evidence_title') }}</h3>
        <p v-if="unavailableEvidence.length === 0" class="text-sm text-muted-foreground">
          {{ $t('views.DeterminationView.unavailable_evidence_empty') }}
        </p>
        <ul v-else class="space-y-1 text-sm">
          <li
            v-for="entry in unavailableEvidence"
            :key="`${entry.source_id}-${entry.requested_resource}`"
            class="rounded-lg border border-destructive/40 bg-destructive/5 p-3"
            data-testid="determination-unavailable-row"
          >
            <p class="font-mono text-xs break-all">{{ entry.source_id }}</p>
            <p class="text-muted-foreground">
              {{ $t('views.DeterminationView.unavailable_requested_resource') }}: {{ entry.requested_resource }}
            </p>
            <p>{{ entry.reason }}</p>
          </li>
        </ul>
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
            <span class="font-mono break-all">{{ reference.locator }}</span>
            <span class="mx-1">·</span>
            <span>{{ $t('views.DeterminationView.provenance_' + reference.provenance) }}</span>
          </li>
        </ul>
      </section>

      <section class="space-y-2" data-testid="determination-source-inventory">
        <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.source_inventory_title') }}</h3>
        <ul class="space-y-1 text-sm">
          <li
            v-for="source in sources"
            :key="source.source_id"
            class="flex flex-wrap items-baseline gap-2 rounded-lg border bg-muted/20 p-3"
            data-testid="determination-source-row"
          >
            <span class="font-mono text-xs break-all">{{ source.source_id }}</span>
            <span class="text-muted-foreground">{{ source.connector_type }} · {{ source.resource }} · {{ source.sample_count }}</span>
            <span class="rounded-full px-2 py-0.5 text-xs" :class="source.status === 'available' ? 'bg-success/10 text-success-text' : 'bg-destructive/10 text-destructive-text'">
              {{ $t('views.DeterminationView.source_status_' + source.status) }}
            </span>
            <span v-if="source.error" class="text-xs text-destructive-text">{{ source.error }}</span>
          </li>
        </ul>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ErrorAlert from '../shared/ErrorAlert.vue'
import LoadingSpinner from '../shared/LoadingSpinner.vue'
import DeterminationEvidenceRefs from './DeterminationEvidenceRefs.vue'
import { confidenceBadgeClass } from './confidencePresentation'
import type { DeterminationProposal } from '../../composables/useDetermination'

const props = defineProps<{
  proposal: DeterminationProposal | null
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{ (event: 'retry'): void }>()

const uncertainty = computed(() => props.proposal?.uncertainty ?? [])
const observedFacts = computed(() => props.proposal?.observed_facts ?? [])
const derivedElements = computed(() => props.proposal?.derived_elements ?? [])
const assumptions = computed(() => props.proposal?.unresolved_assumptions ?? [])
const unavailableEvidence = computed(() => props.proposal?.unavailable_evidence ?? [])
const provenance = computed(() => props.proposal?.provenance ?? [])
const sources = computed(() => props.proposal?.attempt.source_inventory ?? [])

function onRetry(): void {
  emit('retry')
}
</script>
