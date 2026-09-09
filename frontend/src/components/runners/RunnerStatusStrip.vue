<template>
  <div
    class="rounded-lg border px-4 py-3 flex items-center gap-2.5 text-sm"
    :class="stripClass"
    role="status"
    data-testid="runner-status-strip"
  >
    <span
      class="inline-block h-2 w-2 rounded-full shrink-0"
      :class="dotClass"
      aria-hidden="true"
    />
    <span class="font-medium" data-testid="runner-status-strip-state">{{ stateLabel }}</span>
    <span v-if="detail" class="text-muted-foreground">{{ detail }}</span>
    <span v-if="showMachineCount" class="ml-auto text-xs text-muted-foreground" data-testid="runner-status-strip-machines">
      {{ $t('views.RunnersStatusStrip.machine_count', { count: machines.length }) }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { RunnersStatus, StripState } from '../../lib/runnersStatus'

const { t } = useI18n()

const props = defineProps<{
  status: RunnersStatus | null | undefined
  /** qa F11: the parent's status fetch failed — render an explicit
   * unavailable strip instead of the perpetually-loading grey one. */
  errored?: boolean
}>()

const machines = computed(() => props.status?.machines ?? [])

const runnerConfigured = computed(() =>
  (props.status?.profiles ?? []).some((p) => p.provider_type === 'runner_docker'),
)

const state = computed<StripState | 'loading' | 'not_enabled' | 'fetch_failed'>(() => {
  if (props.errored) return 'fetch_failed'
  if (!props.status) return 'loading'
  if (!runnerConfigured.value) return 'not_enabled'
  return props.status.aggregate_state
})

const stateLabel = computed(() => {
  switch (state.value) {
    case 'healthy':
      return t('views.RunnersStatusStrip.healthy')
    case 'not_enabled':
      return t('views.RunnersStatusStrip.not_enabled')
    case 'engine_unreachable':
      return t('views.RunnersStatusStrip.engine_unreachable')
    case 'image_not_pulled':
      return t('views.RunnersStatusStrip.image_not_pulled')
    case 'stale':
      return t('views.RunnersStatusStrip.status_unknown', { seconds: staleSeconds.value })
    case 'loading':
      return t('views.RunnersStatusStrip.loading')
    case 'fetch_failed':
      return t('views.RunnersStatusStrip.fetch_failed')
    default:
      // qa F17/F18: an unknown probe state renders AS ITSELF, never as an
      // empty label (the exhaustive switch above makes this unreachable
      // while the wire contract holds).
      return state.value
  }
})

const staleSeconds = computed(() =>
  machines.value.reduce((max, m) => Math.max(max, m.age_seconds), 0),
)

const detail = computed(() => {
  if (state.value === 'engine_unreachable') {
    const firstError = machines.value.find((m) => m.probe_error)?.probe_error
    return firstError ?? null
  }
  return null
})

const showMachineCount = computed(() => machines.value.length > 1)

// qa F18: ONE keyed visual map — tone (dot + text/border) per state, instead
// of two parallel switch objects that could drift apart.
const STATE_TONE: Record<StripState | 'loading' | 'not_enabled' | 'fetch_failed', { strip: string; dot: string }> = {
  healthy: { strip: 'border-success/40 bg-success/10 text-success', dot: 'bg-success' },
  not_enabled: { strip: 'border-border bg-muted/40 text-muted-foreground', dot: 'bg-muted-foreground' },
  engine_unreachable: { strip: 'border-destructive/40 bg-destructive/10 text-destructive', dot: 'bg-destructive' },
  image_not_pulled: { strip: 'border-warning/40 bg-warning/10 text-warning', dot: 'bg-warning' },
  stale: { strip: 'border-border bg-muted/40 text-muted-foreground', dot: 'bg-muted-foreground' },
  loading: { strip: 'border-border bg-muted/40 text-muted-foreground', dot: 'bg-muted-foreground animate-pulse' },
  fetch_failed: { strip: 'border-destructive/40 bg-destructive/10 text-destructive', dot: 'bg-destructive' },
}

const tone = computed(() => STATE_TONE[state.value] ?? STATE_TONE.stale)
const stripClass = computed(() => tone.value.strip)
const dotClass = computed(() => tone.value.dot)
</script>
