<template>
  <div class="mt-2 space-y-1 text-xs" data-testid="determination-comparison-provenance">
    <p class="font-medium text-muted-foreground">{{ heading }}</p>
    <div class="flex flex-wrap items-center gap-2">
      <span
        v-for="kind in bundle.provenance_kinds ?? []"
        :key="kind"
        class="rounded-full px-2 py-0.5"
        :class="provenanceBadgeClass(kind)"
        data-testid="determination-comparison-provenance-kind"
      >
        {{ $t('views.DeterminationView.provenance_' + kind) }}
      </span>
      <span
        class="rounded-full px-2 py-0.5"
        :class="confidenceBadgeClass(bundle.confidence)"
        data-testid="determination-comparison-confidence"
      >
        {{ $t('views.DeterminationView.confidence_' + bundle.confidence) }}
      </span>
    </div>
    <p class="text-muted-foreground">
      <span class="font-medium">{{ $t('views.DeterminationView.comparison_uncertainty') }}:</span>
      <span v-if="uncertainty.length === 0" data-testid="determination-comparison-uncertainty-empty">
        {{ $t('views.DeterminationView.comparison_uncertainty_empty') }}
      </span>
    </p>
    <ul v-if="uncertainty.length > 0" class="list-disc space-y-0.5 pl-5" data-testid="determination-comparison-uncertainty">
      <li v-for="(entry, index) in uncertainty" :key="index">{{ entry }}</li>
    </ul>
    <DeterminationEvidenceRefs :references="bundle.evidence_refs ?? []" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import DeterminationEvidenceRefs from './DeterminationEvidenceRefs.vue'
import { confidenceBadgeClass } from './confidencePresentation'
import { provenanceBadgeClass } from './evidencePresentation'
import type { ProvenanceBundle } from './comparisonPresentation'

/**
 * One side's provenance copied onto a comparison item.
 *
 * The comparison carries provenance per side, never per node or edge, so this
 * block is always labelled with the side it came from - observed/current and
 * proposed/target content must stay distinguishable.
 */
const props = defineProps<{
  heading: string
  bundle: ProvenanceBundle
}>()

const uncertainty = computed(() => props.bundle.uncertainty ?? [])
</script>
