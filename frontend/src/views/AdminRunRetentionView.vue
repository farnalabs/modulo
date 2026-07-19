<template>
  <div data-theme="agent" class="page-wide">
    <PageHeader :title="$t('views.AdminRunRetentionView.run_retention')" :subtitle="$t('views.AdminRunRetentionView.configure_run_retention_policies_and_manual_purge')" />

    <FeatureGate feature-name="admin_run_retention" required-tier="team" show-disabled>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

      <template v-else>
        <Card>
          <CardHeader>
            <CardTitle>Current Retention Period</CardTitle>
            <CardDescription>Runs older than this many days are automatically cleaned up</CardDescription>
          </CardHeader>
          <CardContent>
            <div class="flex items-end gap-3">
              <div class="flex-1">
                <span class="mb-1.5 block text-xs font-medium text-muted-foreground">Retention period (days)</span>
                <Input aria-label="Form control"
                  :model-value="retentionDays ?? undefined"
                  @update:model-value="(v: any) => retentionDays = v === '' ? null : Number(v)"
                  type="number"
                  min="7"
                  max="365"
                  data-testid="admin-run-retention-days"
                />
              </div>
              <Button :disabled="savingRetention" data-testid="admin-run-retention-save" @click="saveRetention">
                {{ savingRetention ? 'Saving...' : 'Save' }}
              </Button>
            </div>
            <p v-if="retentionSaveError" class="mt-2 text-xs text-destructive">{{ retentionSaveError }}</p>
            <p v-if="retentionSaveSuccess" class="mt-2 text-xs text-success">Retention period updated.</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Manual Purge</CardTitle>
            <CardDescription>Immediately delete runs older than a specified number of days</CardDescription>
          </CardHeader>
          <CardContent>
            <div class="flex items-end gap-3">
              <div class="flex-1">
                <span class="mb-1.5 block text-xs font-medium text-muted-foreground">Purge runs older than (days)</span>
                <Input aria-label="Form control"
                  :model-value="purgeAge ?? undefined"
                  @update:model-value="(v: any) => purgeAge = v === '' ? null : Number(v)"
                  type="number"
                  min="1"
                  max="365"
                  data-testid="admin-run-retention-purge-age"
                />
              </div>
              <Button
                :disabled="purging"
                variant="destructive"
                data-testid="admin-run-retention-purge-now"
                @click="executePurge"
              >
                {{ purging ? 'Purging...' : 'Purge Now' }}
              </Button>
            </div>
            <p v-if="purgeError" class="mt-2 text-xs text-destructive">{{ purgeError }}</p>
            <p v-if="purgeResult" class="mt-2 text-xs text-success">{{ purgeResult }}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Storage Info</CardTitle>
            <CardDescription>Current run storage statistics</CardDescription>
          </CardHeader>
          <CardContent>
            <LoadingSpinner v-if="storageLoading" />
            <div v-else-if="storageError" class="text-sm text-destructive">{{ storageError }}</div>
            <div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <div class="rounded-lg border bg-muted p-4 text-center">
                <p class="text-2xl font-semibold" data-testid="admin-run-retention-total-runs">{{ storageInfo.total_runs ?? 0 }}</p>
                <p class="text-xs text-muted-foreground">Total Runs</p>
              </div>
              <div class="rounded-lg border bg-muted p-4 text-center">
                <p class="text-2xl font-semibold">{{ storageInfo.status_breakdown ? Object.keys(storageInfo.status_breakdown).length : 0 }}</p>
                <p class="text-xs text-muted-foreground">Status Categories</p>
              </div>
              <div class="rounded-lg border bg-muted p-4 text-center">
                <p class="text-2xl font-semibold" data-testid="admin-run-retention-estimated-saved">{{ storageInfo.estimated_saved_bytes ? formatBytes(storageInfo.estimated_saved_bytes) : '0 B' }}</p>
                <p class="text-xs text-muted-foreground">Estimated Storage Saved</p>
              </div>
            </div>
            <div v-if="storageInfo.status_breakdown && Object.keys(storageInfo.status_breakdown).length > 0" class="mt-4">
              <h4 class="mb-2 text-sm font-medium text-muted-foreground">Runs by Status</h4>
              <div class="space-y-1">
                <div v-for="(count, status) in storageInfo.status_breakdown" :key="status" class="flex items-center justify-between rounded border px-3 py-2 text-sm">
                  <span class="capitalize">{{ status }}</span>
                  <span class="font-medium">{{ count }}</span>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      </template>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '../components/ui/card'
import { Input } from '../components/ui/input'
import { Button } from '../components/ui/button'

const planStore = usePlanStore()

interface RetentionConfig {
  retention_days: number
}

interface StorageInfo {
  total_runs: number
  status_breakdown: Record<string, number>
  estimated_saved_bytes: number
}

interface PurgeResult {
  deleted_count: number
}

const { data: retentionData, loading, error: loadError, load: loadData } = useDataFetch(
  () => (api as any).GET('/api/v1/admin/runs/retention') as Promise<{ data?: RetentionConfig; error?: { detail?: string } }>,
)

const retentionDays = computed(() => retentionData.value?.retention_days ?? null)
const savingRetention = ref(false)
const retentionSaveError = ref<string | null>(null)
const retentionSaveSuccess = ref(false)

const { data: storageResp, loading: storageLoading, error: storageError, load: loadStorageInfo } = useDataFetch(
  () => (api as any).GET('/api/v1/admin/runs/storage') as Promise<{ data?: StorageInfo; error?: { detail?: string } }>,
  { initialValue: { total_runs: 0, status_breakdown: {}, estimated_saved_bytes: 0 } as StorageInfo }
)

const storageInfo = computed(() => storageResp.value ?? { total_runs: 0, status_breakdown: {}, estimated_saved_bytes: 0 } as StorageInfo)

const purgeAge = ref<number | null>(null)
const purging = ref(false)
const purgeError = ref<string | null>(null)
const purgeResult = ref<string | null>(null)

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  const val = bytes / Math.pow(1024, i)
  return `${val.toFixed(1)} ${units[i]}`
}

async function saveRetention() {
  savingRetention.value = true
  retentionSaveError.value = null
  retentionSaveSuccess.value = false
  try {
    const { error: err } = await (api as any).PUT('/api/v1/admin/runs/retention', {
      body: { retention_days: retentionDays.value },
    })
    if (err) {
      retentionSaveError.value = `Failed to save: ${formatApiError(err)}`
    } else {
      retentionSaveSuccess.value = true
    }
  } catch (e: unknown) {
    retentionSaveError.value = `Failed to save: ${formatApiError(e)}`
  } finally {
    savingRetention.value = false
  }
}

async function executePurge() {
  if (!purgeAge.value || purgeAge.value < 1) {
    purgeError.value = 'Please enter a valid number of days.'
    return
  }
  if (!window.confirm(`This will permanently delete all runs older than ${purgeAge.value} days. Continue?`)) {
    return
  }
  purging.value = true
  purgeError.value = null
  purgeResult.value = null
  try {
    const { data, error: err } = await (api as any).POST('/api/v1/admin/runs/purge', {
      body: { older_than_days: purgeAge.value },
    })
    if (err) {
      purgeError.value = `Purge failed: ${formatApiError(err)}`
    } else if (data) {
      const resp = data as PurgeResult
      purgeResult.value = `Purge completed. ${resp.deleted_count} run(s) deleted.`
      loadStorageInfo()
    }
  } catch (e: unknown) {
    purgeError.value = `Purge failed: ${formatApiError(e)}`
  } finally {
    purging.value = false
  }
}

planStore.fetchPlan()
</script>
