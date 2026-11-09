<template>
  <section class="card p-4 space-y-5" data-testid="determination-comparison">
    <header class="space-y-1">
      <h2 class="text-lg font-semibold">{{ $t('views.DeterminationView.comparison_title') }}</h2>
      <p class="text-sm text-muted-foreground">{{ $t('views.DeterminationView.comparison_hint') }}</p>
    </header>

    <p
      class="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm"
      data-testid="determination-comparison-not-activated"
    >
      {{ $t('views.DeterminationView.comparison_not_activated') }}
    </p>

    <section class="rounded-lg border p-3 space-y-2" data-testid="determination-comparison-baseline">
      <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.comparison_baseline_title') }}</h3>
      <template v-if="baseline">
        <dl class="grid gap-2 text-sm sm:grid-cols-2">
          <div>
            <dt class="text-muted-foreground">{{ $t('views.DeterminationView.comparison_baseline_attempt') }}</dt>
            <dd class="font-mono text-xs break-all" data-testid="determination-comparison-baseline-attempt">
              {{ baseline.proposal.attempt.id }}
            </dd>
          </div>
          <div>
            <dt class="text-muted-foreground">{{ $t('views.DeterminationView.comparison_baseline_proposal') }}</dt>
            <dd class="font-mono text-xs break-all" data-testid="determination-comparison-baseline-proposal">
              {{ baseline.proposal.proposal_id }}
            </dd>
          </div>
        </dl>
        <p class="text-xs text-muted-foreground">{{ $t('views.DeterminationView.comparison_baseline_hint') }}</p>
      </template>
      <p
        v-else
        class="text-sm text-muted-foreground"
        data-testid="determination-comparison-baseline-empty"
      >
        {{ $t('views.DeterminationView.comparison_baseline_empty') }}
      </p>
    </section>

    <div class="flex flex-wrap gap-2">
      <button
        type="button"
        class="rounded-md border px-3 py-2 text-sm font-medium disabled:opacity-60"
        :disabled="!target"
        data-testid="determination-comparison-hold"
        @click="emit('hold')"
      >
        {{ baseline ? $t('views.DeterminationView.comparison_replace') : $t('views.DeterminationView.comparison_hold') }}
      </button>
      <button
        type="button"
        class="rounded-md border px-3 py-2 text-sm font-medium disabled:opacity-60"
        :disabled="!baseline"
        data-testid="determination-comparison-clear"
        @click="emit('clear')"
      >
        {{ $t('views.DeterminationView.comparison_clear') }}
      </button>
      <button
        type="button"
        class="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-60"
        :disabled="!baseline || !target || loading"
        data-testid="determination-comparison-run"
        @click="emit('compare')"
      >
        {{ loading ? $t('views.DeterminationView.comparison_running') : $t('views.DeterminationView.comparison_run') }}
      </button>
    </div>

    <p class="text-xs text-muted-foreground">
      <span v-if="!target">{{ $t('views.DeterminationView.comparison_needs_target') }}</span>
      <span v-else>
        {{ $t('views.DeterminationView.comparison_target_attempt', { attempt: target.proposal.attempt.id }) }}
      </span>
    </p>

    <div
      v-if="loading"
      class="flex items-center gap-3 text-sm text-muted-foreground"
      aria-live="polite"
      data-testid="determination-comparison-loading"
    >
      <LoadingSpinner />
      <span>{{ $t('views.DeterminationView.comparison_loading') }}</span>
    </div>

    <div
      v-else-if="error"
      role="alert"
      aria-live="assertive"
      class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-destructive-text"
      data-testid="determination-comparison-error"
    >
      <p class="text-sm font-medium">{{ $t('views.DeterminationView.comparison_failed') }}</p>
      <p class="text-sm">{{ error }}</p>
    </div>

    <p
      v-else-if="!comparison"
      class="text-sm text-muted-foreground"
      data-testid="determination-comparison-idle"
    >
      {{ $t('views.DeterminationView.comparison_idle') }}
    </p>

    <template v-else>
      <section class="grid gap-3 sm:grid-cols-2" aria-live="polite" data-testid="determination-comparison-sides">
        <article
          v-for="side in sides"
          :key="side.side"
          class="rounded-lg border bg-muted/20 p-3 space-y-1"
          data-testid="determination-comparison-side"
        >
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.comparison_side_' + side.side) }}</h3>
            <span
              class="rounded-full px-2 py-0.5 text-xs font-medium"
              :class="sideBadgeClass(side.side)"
              data-testid="determination-comparison-side-badge"
            >
              {{ $t('views.DeterminationView.comparison_side_' + side.side) }}
            </span>
          </div>
          <p class="text-xs text-muted-foreground">
            {{ $t('views.DeterminationView.comparison_side_attempt') }}:
            <span class="font-mono break-all" data-testid="determination-comparison-side-attempt">{{ side.attempt_id }}</span>
          </p>
          <p class="text-xs text-muted-foreground">
            {{ $t('views.DeterminationView.comparison_side_proposal') }}:
            <span class="font-mono break-all" data-testid="determination-comparison-side-proposal">{{ side.proposal_id }}</span>
          </p>
          <div class="flex flex-wrap items-center gap-2 text-xs">
            <span
              class="rounded-full px-2 py-0.5"
              :class="confidenceBadgeClass(side.confidence)"
              data-testid="determination-comparison-side-confidence"
            >
              {{ $t('views.DeterminationView.confidence_' + side.confidence) }}
            </span>
            <span class="rounded-full bg-muted px-2 py-0.5">
              {{ $t('views.DeterminationView.review_state_' + side.review_state) }}
            </span>
          </div>
        </article>
      </section>

      <p
        v-if="isEmpty"
        class="rounded-lg border border-success/40 bg-success/5 p-3 text-sm"
        data-testid="determination-comparison-identical"
      >
        {{ $t('views.DeterminationView.comparison_identical') }}
      </p>

      <template v-else>
        <section class="space-y-2" data-testid="determination-comparison-behaviour">
          <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.comparison_behaviour_title') }}</h3>
          <p
            v-if="behaviourChanges.length === 0"
            class="text-sm text-muted-foreground"
            data-testid="determination-comparison-behaviour-empty"
          >
            {{ $t('views.DeterminationView.comparison_behaviour_empty') }}
          </p>
          <ul v-else class="space-y-2">
            <li
              v-for="change in behaviourChanges"
              :key="behaviourKey(change)"
              class="rounded-lg border bg-muted/20 p-3 space-y-1"
              data-testid="determination-comparison-behaviour-row"
            >
              <div class="flex flex-wrap items-center justify-between gap-2">
                <p class="text-sm font-medium">{{ behaviourLabel(change.kind) }}</p>
                <span class="rounded-full px-2 py-0.5 text-xs" :class="sideBadgeClass('target')">
                  {{ $t('views.DeterminationView.comparison_side_target') }}
                </span>
              </div>
              <p class="text-xs text-muted-foreground">
                {{ $t('views.DeterminationView.comparison_subject') }}:
                {{ $t('views.DeterminationView.comparison_subject_' + change.subject_kind) }}
                <span class="font-mono break-all" data-testid="determination-comparison-item-id">{{ change.subject_id }}</span>
              </p>
              <p class="text-xs">
                <span class="text-muted-foreground">{{ $t('views.DeterminationView.comparison_property') }}:</span>
                <span class="font-mono">{{ change.property }}</span>
                <span class="mx-1">·</span>
                <span class="text-muted-foreground">{{ $t('views.DeterminationView.comparison_current') }}:</span>
                {{ valueText(change.base_value) }}
                <span class="mx-1">→</span>
                <span class="text-muted-foreground">{{ $t('views.DeterminationView.comparison_target') }}:</span>
                {{ valueText(change.target_value) }}
              </p>
            </li>
          </ul>
        </section>

        <section v-if="nodesAdded.length > 0" class="space-y-2" data-testid="determination-comparison-nodes-added">
          <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.comparison_nodes_added_title') }}</h3>
          <ul class="space-y-2">
            <li
              v-for="item in nodesAdded"
              :key="item.id"
              class="rounded-lg border bg-muted/20 p-3 space-y-1"
              data-testid="determination-comparison-node-added"
            >
              <div class="flex flex-wrap items-center justify-between gap-2">
                <p class="text-sm">
                  <span class="font-mono" data-testid="determination-comparison-item-id">{{ item.id }}</span>
                  <span class="mx-1">·</span>
                  <span>{{ item.node.label }}</span>
                </p>
                <span class="rounded-full px-2 py-0.5 text-xs" :class="sideBadgeClass(item.side)" data-testid="determination-comparison-side-badge">
                  {{ $t('views.DeterminationView.comparison_side_' + item.side) }}
                </span>
              </div>
              <p class="text-xs text-muted-foreground">
                {{ $t('views.DeterminationView.comparison_node_type') }}:
                {{ item.node.node_type }}
                <template v-if="item.node.connector_type">
                  <span class="mx-1">·</span>
                  {{ $t('views.DeterminationView.comparison_node_connector') }}:
                  {{ item.node.connector_type }}
                </template>
              </p>
              <DeterminationComparisonProvenance
                :heading="$t('views.DeterminationView.comparison_provenance_target')"
                :bundle="item.provenance"
              />
            </li>
          </ul>
        </section>

        <section v-if="nodesRemoved.length > 0" class="space-y-2" data-testid="determination-comparison-nodes-removed">
          <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.comparison_nodes_removed_title') }}</h3>
          <ul class="space-y-2">
            <li
              v-for="item in nodesRemoved"
              :key="item.id"
              class="rounded-lg border bg-muted/20 p-3 space-y-1"
              data-testid="determination-comparison-node-removed"
            >
              <div class="flex flex-wrap items-center justify-between gap-2">
                <p class="text-sm">
                  <span class="font-mono" data-testid="determination-comparison-item-id">{{ item.id }}</span>
                  <span class="mx-1">·</span>
                  <span>{{ item.node.label }}</span>
                </p>
                <span class="rounded-full px-2 py-0.5 text-xs" :class="sideBadgeClass(item.side)" data-testid="determination-comparison-side-badge">
                  {{ $t('views.DeterminationView.comparison_side_' + item.side) }}
                </span>
              </div>
              <p class="text-xs text-muted-foreground">
                {{ $t('views.DeterminationView.comparison_node_type') }}:
                {{ item.node.node_type }}
              </p>
              <DeterminationComparisonProvenance
                :heading="$t('views.DeterminationView.comparison_provenance_current')"
                :bundle="item.provenance"
              />
            </li>
          </ul>
        </section>

        <section v-if="nodesChanged.length > 0" class="space-y-2" data-testid="determination-comparison-nodes-changed">
          <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.comparison_nodes_changed_title') }}</h3>
          <ul class="space-y-2">
            <li
              v-for="item in nodesChanged"
              :key="item.id"
              class="rounded-lg border bg-muted/20 p-3 space-y-2"
              data-testid="determination-comparison-node-changed"
            >
              <p class="text-sm font-mono break-all" data-testid="determination-comparison-item-id">{{ item.id }}</p>
              <ul class="space-y-1">
                <li
                  v-for="change in item.changes"
                  :key="change.property"
                  class="flex flex-wrap items-center gap-2 text-xs"
                  data-testid="determination-comparison-property"
                >
                  <span class="font-mono" data-testid="determination-comparison-property-name">{{ change.property }}</span>
                  <span class="rounded-full px-2 py-0.5" :class="sideBadgeClass('base')">
                    {{ $t('views.DeterminationView.comparison_current') }}
                  </span>
                  <span data-testid="determination-comparison-property-current">{{ valueText(change.base_value) }}</span>
                  <span>→</span>
                  <span class="rounded-full px-2 py-0.5" :class="sideBadgeClass('target')">
                    {{ $t('views.DeterminationView.comparison_target') }}
                  </span>
                  <span data-testid="determination-comparison-property-target">{{ valueText(change.target_value) }}</span>
                </li>
              </ul>
              <DeterminationComparisonProvenance
                :heading="$t('views.DeterminationView.comparison_provenance_current')"
                :bundle="item.base_provenance"
              />
              <DeterminationComparisonProvenance
                :heading="$t('views.DeterminationView.comparison_provenance_target')"
                :bundle="item.target_provenance"
              />
            </li>
          </ul>
        </section>

        <section v-if="edgesAdded.length > 0" class="space-y-2" data-testid="determination-comparison-edges-added">
          <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.comparison_edges_added_title') }}</h3>
          <ul class="space-y-2">
            <li
              v-for="item in edgesAdded"
              :key="item.id"
              class="rounded-lg border bg-muted/20 p-3 space-y-1"
              data-testid="determination-comparison-edge-added"
            >
              <div class="flex flex-wrap items-center justify-between gap-2">
                <p class="text-sm font-mono break-all" data-testid="determination-comparison-item-id">{{ item.id }}</p>
                <span class="rounded-full px-2 py-0.5 text-xs" :class="sideBadgeClass(item.side)" data-testid="determination-comparison-side-badge">
                  {{ $t('views.DeterminationView.comparison_side_' + item.side) }}
                </span>
              </div>
              <p class="text-xs text-muted-foreground">
                {{ $t('views.DeterminationView.comparison_edge_type') }}:
                {{ item.edge.edge_type }}
                <span class="mx-1">·</span>
                {{ $t('views.DeterminationView.comparison_edge_hitl') }}:
                {{ valueText(item.edge.hitl_review) }}
              </p>
              <DeterminationComparisonProvenance
                :heading="$t('views.DeterminationView.comparison_provenance_target')"
                :bundle="item.provenance"
              />
            </li>
          </ul>
        </section>

        <section v-if="edgesRemoved.length > 0" class="space-y-2" data-testid="determination-comparison-edges-removed">
          <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.comparison_edges_removed_title') }}</h3>
          <ul class="space-y-2">
            <li
              v-for="item in edgesRemoved"
              :key="item.id"
              class="rounded-lg border bg-muted/20 p-3 space-y-1"
              data-testid="determination-comparison-edge-removed"
            >
              <div class="flex flex-wrap items-center justify-between gap-2">
                <p class="text-sm font-mono break-all" data-testid="determination-comparison-item-id">{{ item.id }}</p>
                <span class="rounded-full px-2 py-0.5 text-xs" :class="sideBadgeClass(item.side)" data-testid="determination-comparison-side-badge">
                  {{ $t('views.DeterminationView.comparison_side_' + item.side) }}
                </span>
              </div>
              <p class="text-xs text-muted-foreground">
                {{ $t('views.DeterminationView.comparison_edge_type') }}:
                {{ item.edge.edge_type }}
                <span class="mx-1">·</span>
                {{ $t('views.DeterminationView.comparison_edge_hitl') }}:
                {{ valueText(item.edge.hitl_review) }}
              </p>
              <DeterminationComparisonProvenance
                :heading="$t('views.DeterminationView.comparison_provenance_current')"
                :bundle="item.provenance"
              />
            </li>
          </ul>
        </section>

        <section v-if="edgesChanged.length > 0" class="space-y-2" data-testid="determination-comparison-edges-changed">
          <h3 class="text-sm font-medium">{{ $t('views.DeterminationView.comparison_edges_changed_title') }}</h3>
          <ul class="space-y-2">
            <li
              v-for="item in edgesChanged"
              :key="item.id"
              class="rounded-lg border bg-muted/20 p-3 space-y-2"
              data-testid="determination-comparison-edge-changed"
            >
              <p class="text-sm font-mono break-all" data-testid="determination-comparison-item-id">{{ item.id }}</p>
              <ul class="space-y-1">
                <li
                  v-for="change in item.changes"
                  :key="change.property"
                  class="flex flex-wrap items-center gap-2 text-xs"
                  data-testid="determination-comparison-property"
                >
                  <span class="font-mono" data-testid="determination-comparison-property-name">{{ change.property }}</span>
                  <span class="rounded-full px-2 py-0.5" :class="sideBadgeClass('base')">
                    {{ $t('views.DeterminationView.comparison_current') }}
                  </span>
                  <span data-testid="determination-comparison-property-current">{{ valueText(change.base_value) }}</span>
                  <span>→</span>
                  <span class="rounded-full px-2 py-0.5" :class="sideBadgeClass('target')">
                    {{ $t('views.DeterminationView.comparison_target') }}
                  </span>
                  <span data-testid="determination-comparison-property-target">{{ valueText(change.target_value) }}</span>
                </li>
              </ul>
              <DeterminationComparisonProvenance
                :heading="$t('views.DeterminationView.comparison_provenance_current')"
                :bundle="item.base_provenance"
              />
              <DeterminationComparisonProvenance
                :heading="$t('views.DeterminationView.comparison_provenance_target')"
                :bundle="item.target_provenance"
              />
            </li>
          </ul>
        </section>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import LoadingSpinner from '../shared/LoadingSpinner.vue'
import DeterminationComparisonProvenance from './DeterminationComparisonProvenance.vue'
import { confidenceBadgeClass } from './confidencePresentation'
import {
  behaviourChangeKey,
  behaviourLabelKey,
  comparisonIsEmpty,
  propertyValueView,
  sideBadgeClass,
  type BehaviourChange,
  type ProposalComparison,
} from './comparisonPresentation'
import type { DeterminationDraftResponse } from '../../composables/useDetermination'

/**
 * Migration comparison panel (FAR-2473).
 *
 * The reviewer holds one generated draft as the current baseline and compares
 * it with the latest draft as the proposed target. Every rendered item is a
 * fact the comparison returned - added, removed and changed nodes and edges,
 * their changed properties with both values, and only the approval/automation
 * categories the response contains. The panel never renders a category the
 * backend did not report and never mutates anything.
 */
const props = defineProps<{
  baseline: DeterminationDraftResponse | null
  target: DeterminationDraftResponse | null
  comparison: ProposalComparison | null
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{
  (event: 'hold'): void
  (event: 'clear'): void
  (event: 'compare'): void
}>()

const { t } = useI18n()

const sides = computed(() =>
  props.comparison ? [props.comparison.base, props.comparison.target] : [],
)
const isEmpty = computed(() => (props.comparison ? comparisonIsEmpty(props.comparison) : false))
const behaviourChanges = computed<BehaviourChange[]>(() => props.comparison?.behaviour_changes ?? [])
const nodesAdded = computed(() => props.comparison?.nodes_added ?? [])
const nodesRemoved = computed(() => props.comparison?.nodes_removed ?? [])
const nodesChanged = computed(() => props.comparison?.nodes_changed ?? [])
const edgesAdded = computed(() => props.comparison?.edges_added ?? [])
const edgesRemoved = computed(() => props.comparison?.edges_removed ?? [])
const edgesChanged = computed(() => props.comparison?.edges_changed ?? [])

function behaviourLabel(kind: string): string {
  return t(`views.DeterminationView.${behaviourLabelKey(kind)}`, { kind })
}

function behaviourKey(change: BehaviourChange): string {
  return behaviourChangeKey(change)
}

function valueText(value: string | boolean | null): string {
  const view = propertyValueView(value)
  if (view.kind === 'absent') return t('views.DeterminationView.comparison_value_absent')
  if (view.kind === 'boolean') {
    return view.value
      ? t('views.DeterminationView.comparison_value_true')
      : t('views.DeterminationView.comparison_value_false')
  }
  return view.value
}
</script>
