<template>
  <section class="card p-4 space-y-5" data-testid="determination-proposal">
    <header class="flex flex-wrap items-baseline justify-between gap-2">
      <h2 class="text-lg font-semibold">{{ $t('views.DeterminationView.proposal_title') }}</h2>
      <span v-if="proposal" class="text-xs text-muted-foreground">
        {{ $t('views.DeterminationView.review_state_' + proposal.review_state) }}
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
              <div class="flex flex-wrap items-center gap-2">
                <span
                  class="rounded-full px-2 py-0.5 text-xs font-medium"
                  :class="elementKindBadgeClass('observed')"
                  data-testid="determination-element-kind"
                >
                  {{ $t('views.DeterminationView.element_kind_observed') }}
                </span>
                <p class="text-sm font-medium">{{ fact.statement }}</p>
              </div>
              <span class="rounded-full px-2 py-0.5 text-xs" :class="confidenceBadgeClass(fact.confidence)">
                {{ $t('views.DeterminationView.confidence_' + fact.confidence) }}
              </span>
            </div>
            <DeterminationEvidenceRefs :references="fact.evidence_refs ?? []" />
            <button
              type="button"
              class="mt-2 rounded border px-2 py-1 text-xs"
              :aria-label="$t('views.DeterminationView.evidence_open_for', { statement: fact.statement })"
              data-testid="determination-element-evidence"
              @click="selectElement(elementView(fact, 'observed'))"
            >
              {{ $t('views.DeterminationView.evidence_open') }}
            </button>
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
              <div class="flex flex-wrap items-center gap-2">
                <span
                  class="rounded-full px-2 py-0.5 text-xs font-medium"
                  :class="elementKindBadgeClass('derived')"
                  data-testid="determination-element-kind"
                >
                  {{ $t('views.DeterminationView.element_kind_derived') }}
                </span>
                <p class="text-sm font-medium">{{ element.statement }}</p>
              </div>
              <span class="rounded-full px-2 py-0.5 text-xs" :class="confidenceBadgeClass(element.confidence)">
                {{ $t('views.DeterminationView.confidence_' + element.confidence) }}
              </span>
            </div>
            <DeterminationEvidenceRefs :references="element.evidence_refs ?? []" />
            <button
              type="button"
              class="mt-2 rounded border px-2 py-1 text-xs"
              :aria-label="$t('views.DeterminationView.evidence_open_for', { statement: element.statement })"
              data-testid="determination-element-evidence"
              @click="selectElement(elementView(element, 'derived'))"
            >
              {{ $t('views.DeterminationView.evidence_open') }}
            </button>
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
              <div class="flex flex-wrap items-center gap-2">
                <span
                  class="rounded-full px-2 py-0.5 text-xs font-medium"
                  :class="elementKindBadgeClass('assumption')"
                  data-testid="determination-element-kind"
                >
                  {{ $t('views.DeterminationView.element_kind_assumption') }}
                </span>
                <p class="text-sm font-medium">{{ assumption.statement }}</p>
              </div>
              <span class="rounded-full px-2 py-0.5 text-xs" :class="confidenceBadgeClass(assumption.confidence)">
                {{ $t('views.DeterminationView.confidence_' + assumption.confidence) }}
              </span>
            </div>
            <p class="mt-1 text-xs text-muted-foreground">
              <span class="font-medium">{{ $t('views.DeterminationView.assumption_reason') }}:</span>
              <span v-if="reasonFor(assumption.reason).withheld" class="italic">
                {{ $t('views.DeterminationView.evidence_value_withheld') }}
              </span>
              <span v-else>{{ reasonFor(assumption.reason).text }}</span>
            </p>
            <DeterminationEvidenceRefs :references="assumption.evidence_refs ?? []" />
            <button
              type="button"
              class="mt-2 rounded border px-2 py-1 text-xs"
              :aria-label="$t('views.DeterminationView.evidence_open_for', { statement: assumption.statement })"
              data-testid="determination-element-evidence"
              @click="selectElement(elementView(assumption, 'assumption'))"
            >
              {{ $t('views.DeterminationView.evidence_open') }}
            </button>
          </li>
        </ul>
      </section>

      <section class="space-y-2" data-testid="determination-unavailable-evidence" aria-live="polite">
        <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.unavailable_evidence_title') }}</h3>
        <p class="text-xs text-muted-foreground">{{ $t('views.DeterminationView.unavailable_evidence_hint') }}</p>
        <p v-if="unavailableEvidence.length === 0" class="text-sm text-muted-foreground">
          {{ $t('views.DeterminationView.unavailable_evidence_empty') }}
        </p>
        <ul v-else class="space-y-1 text-sm">
          <li
            v-for="entry in unavailableEvidence"
            :key="unavailableEvidenceKey(entry)"
            class="rounded-lg border border-destructive/40 bg-destructive/5 p-3"
            data-testid="determination-unavailable-row"
          >
            <p class="font-mono text-xs break-all">{{ entry.source_id }}</p>
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
      </section>

      <DeterminationProvenanceTrail :proposal="proposal" :review="review ?? null" />

      <DeterminationEvidencePanel :element="selectedElement" @close="clearSelection" />
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import ErrorAlert from '../shared/ErrorAlert.vue'
import LoadingSpinner from '../shared/LoadingSpinner.vue'
import DeterminationEvidencePanel from './DeterminationEvidencePanel.vue'
import DeterminationEvidenceRefs from './DeterminationEvidenceRefs.vue'
import DeterminationProvenanceTrail from './DeterminationProvenanceTrail.vue'
import { confidenceBadgeClass } from './confidencePresentation'
import {
  displayEvidenceText,
  elementKindBadgeClass,
  unavailableEvidenceKey,
  type DisplayText,
  type EvidenceElementKind,
  type EvidenceElementView,
} from './evidencePresentation'
import type {
  DeterminationProposal,
  DeterminationReviewResponse,
  DerivedElement,
  UnresolvedAssumption,
} from '../../composables/useDetermination'

const props = defineProps<{
  proposal: DeterminationProposal | null
  review?: DeterminationReviewResponse | null
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{ (event: 'retry'): void }>()

const observedFacts = computed(() => props.proposal?.observed_facts ?? [])
const derivedElements = computed(() => props.proposal?.derived_elements ?? [])
const assumptions = computed(() => props.proposal?.unresolved_assumptions ?? [])
const unavailableEvidence = computed(() => props.proposal?.unavailable_evidence ?? [])

/**
 * Selection is held by element ID, never by array position: removing or
 * correcting one element must not move the drill-down onto a different one,
 * and the reference ids shown are the contract's own.
 */
const selectedElementId = ref<string | null>(null)

const selectedElement = computed<EvidenceElementView | null>(() => {
  const id = selectedElementId.value
  if (!id) return null
  const observed = observedFacts.value.find((fact) => fact.id === id)
  if (observed) return elementView(observed, 'observed')
  const derived = derivedElements.value.find((element) => element.id === id)
  if (derived) return elementView(derived, 'derived')
  const assumption = assumptions.value.find((entry) => entry.id === id)
  return assumption ? elementView(assumption, 'assumption') : null
})

function elementView(
  element: DerivedElement | UnresolvedAssumption,
  kind: EvidenceElementKind,
): EvidenceElementView {
  return {
    id: element.id,
    kind,
    statement: element.statement,
    confidence: element.confidence,
    evidenceRefs: element.evidence_refs ?? [],
    // Only an unresolved assumption carries its own uncertainty (its reason);
    // observed and derived facts have no per-element field in the contract.
    uncertainty: 'reason' in element ? element.reason : null,
  }
}

function selectElement(element: EvidenceElementView): void {
  selectedElementId.value = element.id
}

function clearSelection(): void {
  selectedElementId.value = null
}

function reasonFor(reason: string): DisplayText {
  return displayEvidenceText(reason)
}

function onRetry(): void {
  emit('retry')
}
</script>
