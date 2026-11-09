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
        <span>
          {{ $t('views.DeterminationView.evidence_locator') }}:
          <span v-if="locatorFor(reference).withheld" class="italic" data-testid="determination-evidence-withheld">
            {{ $t('views.DeterminationView.evidence_value_withheld') }}
          </span>
          <span v-else class="font-mono break-all">{{ locatorFor(reference).text }}</span>
        </span>
        <span class="mx-1">·</span>
        <span>{{ $t('views.DeterminationView.evidence_provenance') }}:</span>
        <span
          class="ml-1 rounded-full px-2 py-0.5"
          :class="provenanceBadgeClass(reference.provenance)"
          data-testid="determination-evidence-provenance"
        >
          {{ $t('views.DeterminationView.provenance_' + reference.provenance) }}
        </span>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import {
  displayEvidenceText,
  provenanceBadgeClass,
  type DisplayText,
  type EvidenceReference,
} from './evidencePresentation'

defineProps<{ references: EvidenceReference[] }>()

/**
 * The locator is connector-produced free text, so it can carry a credential.
 * The field is always shown; only its value is replaced when it looks like a
 * secret (no ``v-html`` anywhere in this surface - every value is text).
 */
function locatorFor(reference: EvidenceReference): DisplayText {
  return displayEvidenceText(reference.locator)
}
</script>
