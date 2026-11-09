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
        <div class="flex flex-wrap items-center gap-1">
          <span class="font-mono break-all">{{ reference.reference_id }}</span>
          <span>·</span>
          <span>{{ $t('views.DeterminationView.evidence_source_id') }}: <span class="font-mono break-all">{{ reference.source_id }}</span></span>
          <span>·</span>
          <span>
            {{ $t('views.DeterminationView.evidence_locator') }}:
            <span v-if="locatorFor(reference).withheld" class="italic" data-testid="determination-evidence-withheld">
              {{ $t('views.DeterminationView.evidence_value_withheld') }}
            </span>
            <span v-else class="font-mono break-all">{{ locatorFor(reference).text }}</span>
          </span>
          <span>·</span>
          <span>{{ $t('views.DeterminationView.evidence_provenance') }}:</span>
          <span
            class="ml-1 rounded-full px-2 py-0.5"
            :class="provenanceBadgeClass(reference.provenance)"
            data-testid="determination-evidence-provenance"
          >
            {{ $t('views.DeterminationView.provenance_' + reference.provenance) }}
          </span>
          <button
            type="button"
            class="ml-auto rounded border px-1.5 py-0.5"
            :aria-expanded="isExpanded(reference.reference_id) ? 'true' : 'false'"
            :aria-controls="detailId(reference.reference_id)"
            data-testid="determination-evidence-toggle"
            @click="toggle(reference.reference_id)"
          >
            {{ isExpanded(reference.reference_id) ? $t('views.DeterminationView.evidence_hide_source') : $t('views.DeterminationView.evidence_show_source') }}
          </button>
        </div>

        <div
          v-if="isExpanded(reference.reference_id)"
          :id="detailId(reference.reference_id)"
          class="mt-2 rounded border bg-muted/30 p-2"
          data-testid="determination-evidence-drilldown"
        >
          <template v-if="sourceFor(reference.source_id)">
            <p class="font-medium">{{ $t('views.DeterminationView.evidence_source_title') }}</p>
            <dl class="mt-1 grid gap-1 sm:grid-cols-2" data-testid="determination-evidence-source">
              <div>
                <dt class="text-muted-foreground">{{ $t('views.DeterminationView.evidence_source_connector') }}</dt>
                <dd class="font-mono break-all">{{ sourceFor(reference.source_id)?.connector_type }}</dd>
              </div>
              <div>
                <dt class="text-muted-foreground">{{ $t('views.DeterminationView.evidence_source_resource') }}</dt>
                <dd class="font-mono break-all">{{ sourceFor(reference.source_id)?.resource }}</dd>
              </div>
              <div>
                <dt class="text-muted-foreground">{{ $t('views.DeterminationView.evidence_source_records') }}</dt>
                <dd>{{ sourceFor(reference.source_id)?.sample_count }}</dd>
              </div>
              <div>
                <dt class="text-muted-foreground">{{ $t('views.DeterminationView.evidence_source_status') }}</dt>
                <dd>{{ $t('views.DeterminationView.source_status_' + sourceFor(reference.source_id)?.status) }}</dd>
              </div>
              <div v-if="sourceFor(reference.source_id)?.error" class="sm:col-span-2">
                <dt class="text-muted-foreground">{{ $t('views.DeterminationView.evidence_source_error') }}</dt>
                <dd class="text-destructive-text">
                  <span v-if="sourceErrorFor(reference.source_id).withheld" class="italic" data-testid="determination-evidence-withheld">
                    {{ $t('views.DeterminationView.evidence_value_withheld') }}
                  </span>
                  <span v-else>{{ sourceErrorFor(reference.source_id).text }}</span>
                </dd>
              </div>
            </dl>
            <button
              type="button"
              class="mt-2 rounded border px-1.5 py-0.5"
              data-testid="determination-evidence-link-source"
              @click="emit('link-source', reference.source_id)"
            >
              {{ $t('views.DeterminationView.evidence_link_source') }}
            </button>
          </template>
          <p v-else class="text-muted-foreground" data-testid="determination-evidence-source-missing">
            {{ $t('views.DeterminationView.evidence_source_missing') }}
          </p>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import {
  displayEvidenceText,
  provenanceBadgeClass,
  type DisplayText,
  type EvidenceReference,
} from './evidencePresentation'
import type { DeterminationSourceInventoryItem } from '../../composables/useDetermination'

const props = withDefaults(
  defineProps<{
    references: EvidenceReference[]
    sources?: DeterminationSourceInventoryItem[]
  }>(),
  { sources: () => [] },
)

const emit = defineEmits<{ (event: 'link-source', sourceId: string): void }>()

const expanded = ref<Set<string>>(new Set())

function isExpanded(referenceId: string): boolean {
  return expanded.value.has(referenceId)
}

function toggle(referenceId: string): void {
  const next = new Set(expanded.value)
  if (next.has(referenceId)) next.delete(referenceId)
  else next.add(referenceId)
  expanded.value = next
}

function sourceFor(sourceId: string): DeterminationSourceInventoryItem | null {
  return props.sources.find((source) => source.source_id === sourceId) ?? null
}

/**
 * The connector error in the drill-down is free text too, so it is subject to
 * the same credential withholding as every other connector-produced value.
 */
function sourceErrorFor(sourceId: string): DisplayText {
  return displayEvidenceText(sourceFor(sourceId)?.error ?? '')
}

function detailId(referenceId: string): string {
  return `determination-evidence-detail-${referenceId.replace(/[^a-zA-Z0-9_-]/g, '-')}`
}

/**
 * The locator is connector-produced free text, so it can carry a credential.
 * The field is always shown; only its value is replaced when it looks like a
 * secret (no ``v-html`` anywhere in this surface - every value is text).
 */
function locatorFor(reference: EvidenceReference): DisplayText {
  return displayEvidenceText(reference.locator)
}
</script>
