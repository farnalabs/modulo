<template>
  <div data-theme="agent" class="page-wide">
    <PageHeader
      :title="$t('views.AdminSandboxConcurrencyView.max_concurrent_sandbox_runs')"
      :subtitle="$t('views.AdminSandboxConcurrencyView.limit_how_many_sandbox_agent_runs_execute_at_once_across_the_org')"
    />

    <FeatureGate feature-name="environment_profiles" required-tier="team" show-disabled>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

      <template v-else>
        <Card>
          <template #title>{{ $t('views.AdminSandboxConcurrencyView.max_concurrent_sandbox_runs') }}</template>
<template #subtitle>{{ $t('views.AdminSandboxConcurrencyView.leave_empty_for_unlimited') }}</template>

          <template #content>
            <div class="flex items-end gap-3">
              <div class="flex-1">
                <span class="mb-1.5 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminSandboxConcurrencyView.concurrent_run_limit') }}</span>
                <InputText aria-label="Form control"
                  :model-value="limitInput == null ? '' : String(limitInput)"
                  @update:model-value="(v: any) => limitInput = v === '' ? null : Number(v)"
                  type="number"
                  min="1"
                  max="100"
                  data-testid="admin-sandbox-concurrency-limit"
                />
              </div>
              <Button :disabled="saving" data-testid="admin-sandbox-concurrency-save" @click="saveLimit">
                {{ saving ? $t('views.AdminSandboxConcurrencyView.saving') : $t('views.AdminSandboxConcurrencyView.save') }}
              </Button>
            </div>
            <p v-if="saveError" class="mt-2 text-xs text-destructive">{{ saveError }}</p>
            <p v-if="saveSuccess" class="mt-2 text-xs text-success">{{ $t('views.AdminSandboxConcurrencyView.limit_updated') }}</p>
          </template>
        </Card>
      </template>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { ref, watch } from 'vue'
import { api, type components } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Card from 'primevue/card'
import InputText from 'primevue/inputtext'
import Button from 'primevue/button'

const planStore = usePlanStore()

type SandboxConcurrencyConfig = components['schemas']['SandboxConcurrencyResponse']

const { data: limitData, loading, error: loadError, load: loadData } = useDataFetch<SandboxConcurrencyConfig>(
  () => api.GET('/api/v1/admin/org/sandbox-concurrency'),
)

const limitInput = ref<number | null>(null)

watch(limitData, (d) => {
  limitInput.value = d?.sandbox_concurrency_limit ?? null
}, { immediate: true })

const saving = ref(false)
const saveError = ref<string | null>(null)
const saveSuccess = ref(false)

async function saveLimit() {
  const clamped = limitInput.value !== null ? Math.min(100, Math.max(1, limitInput.value)) : null
  limitInput.value = clamped
  saving.value = true
  saveError.value = null
  saveSuccess.value = false
  try {
    const { error: err } = await api.PUT('/api/v1/admin/org/sandbox-concurrency', {
      body: { sandbox_concurrency_limit: clamped },
    })
    if (err) {
      saveError.value = `Failed to save: ${formatApiError(err)}`
    } else {
      saveSuccess.value = true
    }
  } catch (e: unknown) {
    saveError.value = `Failed to save: ${formatApiError(e)}`
  } finally {
    saving.value = false
  }
}

planStore.fetchPlan()
</script>
