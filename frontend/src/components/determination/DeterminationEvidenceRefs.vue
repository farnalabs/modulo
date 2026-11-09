<template>
  <div class="mt-1 text-xs" data-testid="determination-evidence-refs">
    <p v-if="references.length === 0" class="text-muted-foreground" data-testid="determination-evidence-empty">
      {{ $t('views.DeterminationView.evidence_refs_empty') }}
    </p>
    <ul v-else class="space-y-1 text-muted-foreground">
      <li
        v-for="reference in references"
        :key="reference.reference_id"
        class="rounded border bg-background px-2 py-1"
        data-testid="determination-evidence-ref"
      >
        <span class="font-mono break-all">{{ reference.reference_id }}</span>
        <span class="mx-1">·</span>
        <span>{{ $t('views.DeterminationView.evidence_source_id') }}: <span class="font-mono break-all">{{ reference.source_id }}</span></span>
        <span class="mx-1">·</span>
        <span>{{ $t('views.DeterminationView.evidence_locator') }}: <span class="font-mono break-all">{{ reference.locator }}</span></span>
        <span class="mx-1">·</span>
        <span>{{ $t('views.DeterminationView.evidence_provenance') }}: {{ $t('views.DeterminationView.provenance_' + reference.provenance) }}</span>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import type { EvidenceReference } from '../../composables/useDetermination'

defineProps<{ references: EvidenceReference[] }>()
</script>
