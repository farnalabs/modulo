<template>
  <button
    type="button"
    :data-testid="`journey-card-${journey.kind}-${journey.ref}`"
    :aria-label="$t('views.LifecycleMapView.journey.open', { label: refLabel })"
    class="journey-card w-full rounded-md border border-border bg-card px-2 py-1.5 text-left shadow-sm transition-colors hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 outline-none cursor-pointer"
    :class="{ 'border-dashed opacity-60 cursor-pointer': journey.unattributed }"
    @click="$emit('open')"
  >
    <div class="flex items-center justify-between gap-1">
      <span class="truncate text-[11px] font-semibold text-foreground underline decoration-dotted underline-offset-2">
        {{ refLabel }}
      </span>
      <span
        v-if="journey.run_count > 0"
        class="shrink-0 text-[10px] text-muted-foreground"
        :title="$t('views.LifecycleMapView.journey.run_count', { count: journey.run_count })"
      >
        ×{{ journey.run_count }}
      </span>
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width="12"
        height="12"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        class="shrink-0 text-muted-foreground"
      >
        <path d="m9 18 6-6-6-6" />
      </svg>
    </div>
    <div class="mt-1 flex flex-wrap items-center gap-1">
      <span
        v-if="journey.unattributed"
        :data-testid="`journey-unattributed-${journey.kind}-${journey.ref}`"
        class="badge badge-context-slate"
      >
        {{ $t('views.LifecycleMapView.journey.unattributed') }}
      </span>
      <span
        v-else-if="journey.status"
        :class="statusBadgeClass(journey.status)"
        :data-testid="`journey-status-${journey.status}`"
        class="badge capitalize"
      >
        {{ statusLabel(journey.status) }}
      </span>
      <ProvenanceBadge :provenance="journey.provenance" />
    </div>
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { JourneySummary } from '../../types/lifecycleMap'
import { formatRefLabel } from '../../stores/lifecycleMaps'
import ProvenanceBadge from './ProvenanceBadge.vue'

const props = defineProps<{
  journey: JourneySummary
}>()

defineEmits<{
  (e: 'open'): void
}>()

const { t } = useI18n()

const refLabel = computed(() => formatRefLabel(props.journey.kind, props.journey.ref))

function statusBadgeClass(status: string): string {
  switch (status) {
    case 'complete':
      return 'badge-context-green'
    case 'failed':
    case 'stalled':
    case 'eval_failed':
      return 'badge-context-rose'
    case 'running':
      return 'badge-context-blue'
    case 'awaiting_human':
    case 'claimed':
      return 'badge-context-amber'
    default:
      return 'badge-context-slate'
  }
}

function statusLabel(status: string): string {
  const key = `views.LifecycleMapView.journey.status.${status}`
  const translated = t(key)
  return translated === key ? status : translated
}
</script>
