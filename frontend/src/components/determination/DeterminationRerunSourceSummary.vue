<template>
  <div class="space-y-0.5">
    <p class="font-mono break-all">{{ item.source_id }}</p>
    <p class="text-muted-foreground">
      {{ $t('views.DeterminationView.source_connector_type') }}: {{ item.connector_type }}
      <span class="mx-1">·</span>
      {{ $t('views.DeterminationView.source_resource') }}: {{ item.resource }}
      <span class="mx-1">·</span>
      {{ $t('views.DeterminationView.source_sample_count') }}: {{ item.sample_count }}
    </p>
    <p>
      <span
        class="rounded-full px-2 py-0.5"
        :class="item.status === 'available' ? 'bg-success/10 text-success-text' : 'bg-destructive/10 text-destructive-text'"
      >
        {{ $t('views.DeterminationView.source_status_' + item.status) }}
      </span>
    </p>
    <p v-if="item.error" class="text-destructive-text">
      {{ $t('views.DeterminationView.source_error') }}:
      <span v-if="errorText.withheld" class="italic">{{ $t('views.DeterminationView.evidence_value_withheld') }}</span>
      <span v-else>{{ errorText.text }}</span>
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { displayEvidenceText } from './evidencePresentation'
import type { SourceInventoryItem } from './rerunPresentation'

/**
 * One source-inventory item, rendered for a rerun input-change row.
 *
 * Reuses the proposal panel's own source vocabulary (connector type, resource,
 * sample count, availability) rather than inventing a second presentation, and
 * withholds a connector error that looks like a credential.
 */
const props = defineProps<{ item: SourceInventoryItem }>()

const errorText = computed(() => displayEvidenceText(props.item.error ?? ''))
</script>
