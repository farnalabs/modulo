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
import type { RunnersStatus } from '../../lib/runnersStatus'

const { t } = useI18n()

const props = defineProps<{
  status: RunnersStatus | null | undefined
}>()

const machines = computed(() => props.status?.machines ?? [])

const runnerConfigured = computed(() =>
  (props.status?.profiles ?? []).some((p) => p.provider_type === 'runner_docker'),
)

const state = computed(() => {
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
    default:
      return ''
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

const stripClass = computed(() => ({
  healthy: 'border-success/40 bg-success/10 text-success',
  not_enabled: 'border-border bg-muted/40 text-muted-foreground',
  engine_unreachable: 'border-destructive/40 bg-destructive/10 text-destructive',
  image_not_pulled: 'border-warning/40 bg-warning/10 text-warning',
  stale: 'border-border bg-muted/40 text-muted-foreground',
  loading: 'border-border bg-muted/40 text-muted-foreground',
}[state.value]))

const dotClass = computed(() => ({
  healthy: 'bg-success',
  not_enabled: 'bg-muted-foreground',
  engine_unreachable: 'bg-destructive',
  image_not_pulled: 'bg-warning',
  stale: 'bg-muted-foreground',
  loading: 'bg-muted-foreground animate-pulse',
}[state.value]))
</script>
