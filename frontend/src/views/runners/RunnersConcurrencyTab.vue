<template>
  <div class="space-y-6">
    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

    <template v-else>
      <Card>
        <template #title>{{ $t('views.RunnersConcurrencyTab.max_concurrent_runner_runs') }}</template>
        <template #subtitle>{{ $t('views.RunnersConcurrencyTab.semantics_hint') }}</template>

        <template #content>
          <div class="flex items-end gap-3">
            <div class="flex-1 sm:max-w-xs">
              <label for="runner-concurrency-limit" class="mb-1.5 block text-xs font-medium text-muted-foreground">
                {{ $t('views.RunnersConcurrencyTab.concurrent_run_limit') }}
              </label>
              <InputText
                id="runner-concurrency-limit"
                :aria-label="$t('views.RunnersConcurrencyTab.concurrency_limit_aria')"
                :model-value="limitInput == null ? '' : String(limitInput)"
                @update:model-value="(v: any) => limitInput = v === '' ? null : Number(v)"
                type="number"
                min="0"
                max="100"
                data-testid="admin-sandbox-concurrency-limit"
              />
            </div>
            <Button :disabled="saving" data-testid="admin-sandbox-concurrency-save" @click="saveLimit">
              {{ saving ? $t('views.RunnersConcurrencyTab.saving') : $t('views.RunnersConcurrencyTab.save') }}
            </Button>
          </div>

          <p v-if="saveError" class="mt-2 text-xs text-destructive">{{ saveError }}</p>
          <p v-if="saveSuccess" class="mt-2 text-xs text-success">{{ $t('views.RunnersConcurrencyTab.limit_updated') }}</p>

          <div class="mt-4 rounded-lg border border-input bg-muted/30 p-3" data-testid="runner-concurrency-effective">
            <p class="text-xs font-medium">
              {{ effectiveLabel }}
            </p>
            <p class="mt-1 text-xs text-muted-foreground">{{ semanticsLabel }}</p>
          </div>

          <div
            v-if="preflight"
            class="mt-3 rounded-lg border p-3"
            :class="preflightClass"
            data-testid="runner-concurrency-preflight"
          >
            <p class="text-xs font-medium">{{ preflightLabel }}</p>
            <p v-if="preflightDetail" class="mt-1 text-xs text-muted-foreground">{{ preflightDetail }}</p>
          </div>
        </template>
      </Card>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Card from 'primevue/card'
import InputText from 'primevue/inputtext'
import Button from 'primevue/button'
import LoadingSpinner from '../../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../../components/shared/ErrorAlert.vue'
import { api } from '../../lib/api/client'
import { useDataFetch } from '../../composables/useDataFetch'
import { formatApiError } from '../../lib/api/formatError'
import type { ConcurrencyPreflight, RunnersStatus } from '../../lib/runnersStatus'

const props = defineProps<{
  status: RunnersStatus | null | undefined
  reloadStatus: () => Promise<void>
}>()

const { t } = useI18n()

const { data: limitData, loading, error: loadError, load: loadData } = useDataFetch<{
  sandbox_concurrency_limit?: number | null
  is_default: boolean
}>(() => api.GET('/api/v1/admin/org/sandbox-concurrency'))

const limitInput = ref<number | null>(null)

watch(limitData, (d) => {
  limitInput.value = d?.sandbox_concurrency_limit ?? null
}, { immediate: true })

const effective = computed(() => {
  if (!limitData.value) return null
  return { cap: limitData.value.sandbox_concurrency_limit ?? null, isDefault: limitData.value.is_default }
})

const effectiveLabel = computed(() => {
  const e = effective.value
  if (!e) return ''
  if (e.isDefault) {
    return t('views.RunnersConcurrencyTab.effective_default', { value: e.cap })
  }
  if (e.cap === null) {
    return t('views.RunnersConcurrencyTab.effective_no_gate')
  }
  if (e.cap === 0) {
    return t('views.RunnersConcurrencyTab.effective_deny_all')
  }
  return t('views.RunnersConcurrencyTab.effective_explicit', { value: e.cap })
})

const semanticsLabel = computed(() => t('views.RunnersConcurrencyTab.semantics_full'))

const preflight = computed<ConcurrencyPreflight | null>(() => props.status?.concurrency.preflight ?? null)

const preflightLabel = computed(() => {
  const state = preflight.value?.state
  switch (state) {
    case 'ok':
      return t('views.RunnersConcurrencyTab.preflight_ok')
    case 'exceeds_cpu':
      return t('views.RunnersConcurrencyTab.preflight_exceeds_cpu')
    case 'exceeds_mem':
      return t('views.RunnersConcurrencyTab.preflight_exceeds_mem')
    case 'exceeds_cpu_and_mem':
      return t('views.RunnersConcurrencyTab.preflight_exceeds_both')
    case 'uncapped':
      return t('views.RunnersConcurrencyTab.preflight_uncapped')
    case 'unknown':
      return t('views.RunnersConcurrencyTab.preflight_unknown')
    default:
      return state ?? ''
  }
})

const preflightDetail = computed(() => {
  const p = preflight.value
  if (!p) return null
  if (p.engine_cpu_count !== null && p.engine_mem_total_mb !== null) {
    return `${t('views.RunnersConcurrencyTab.engine_resources', { cpu: p.engine_cpu_count, mem: p.engine_mem_total_mb })} — ${t('views.RunnersConcurrencyTab.needed_resources', { cpu: p.needed_cpu, mem: p.needed_mem_mb })}`
  }
  return p.detail
})

const preflightClass = computed(() => {
  const state = preflight.value?.state ?? 'unknown'
  if (state === 'ok') return 'border-success/40 bg-success/10 text-success'
  if (state === 'uncapped' || state === 'unknown') return 'border-border bg-muted/40 text-muted-foreground'
  return 'border-warning/40 bg-warning/10 text-warning'
})

const saving = ref(false)
const saveError = ref<string | null>(null)
const saveSuccess = ref(false)

async function saveLimit() {
  // The REST boundary enforces ge=0 le=100 (FAR-589 D3b); 0 is a meaningful
  // value (deny-all) and is never remapped to "unlimited" here.
  const raw = limitInput.value
  const clamped = raw !== null && !Number.isNaN(raw) ? Math.min(100, Math.max(0, Math.trunc(raw))) : null
  limitInput.value = clamped
  saving.value = true
  saveError.value = null
  saveSuccess.value = false
  try {
    const { error: err } = await api.PUT('/api/v1/admin/org/sandbox-concurrency', {
      body: { sandbox_concurrency_limit: clamped },
    })
    if (err) {
      saveError.value = `${t('views.RunnersConcurrencyTab.save_failed')}: ${formatApiError(err)}`
    } else {
      saveSuccess.value = true
      // qa F6: the effective-cap panel reads the limit via limitData; a
      // successful PUT must refetch the LIMIT SOURCE too (or the panel
      // shows the PRE-save cap until the next manual reload).
      await Promise.all([loadData(), props.reloadStatus()])
    }
  } catch (e: unknown) {
    saveError.value = `${t('views.RunnersConcurrencyTab.save_failed')}: ${formatApiError(e)}`
  } finally {
    saving.value = false
  }
}
</script>
