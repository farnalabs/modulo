<template>
  <FeatureGate feature-name="team_rbac" required-tier="team" show-disabled>

    <div data-theme="agent" class="page-wide">
    <PageHeader title="Organisation Settings" subtitle="Manage your organisation profile, export data, or delete the organisation" />

    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

    <template v-else>
      <div class="space-y-6">
      <!-- Org Info -->
      <SectionCard title="Organisation Info">
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div>
            <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminOrgSettingsView.name') }}</span>
            <p class="mt-0.5 text-lg font-semibold">{{ orgInfo.name }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminOrgSettingsView.slug') }}</span>
            <p class="mt-0.5 font-mono text-sm">{{ orgInfo.slug }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminOrgSettingsView.plan') }}</span>
            <p class="mt-0.5">
              <span :class="orgInfo.planTier === 'team' ? 'badge badge-context-purple' : 'badge badge-status-muted'">
                {{ orgInfo.planTier === 'team' ? 'Team' : 'Community' }}
              </span>
            </p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminOrgSettingsView.created') }}</span>
            <p class="mt-0.5 text-sm font-medium">{{ formatDate(orgInfo.createdAt) }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminOrgSettingsView.members') }}</span>
            <p class="mt-0.5 text-lg font-semibold">{{ orgInfo.memberCount }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminOrgSettingsView.org_id') }}</span>
            <p class="mt-0.5 font-mono text-xs text-muted-foreground">{{ orgInfo.slug || shortId(orgInfo.id) }}</p>
          </div>
        </div>
      </SectionCard>

      <!-- Data Export -->
      <SectionCard title="Data Export" description="Export all organisation data including runs, pipelines, schemas, connectors, and settings.">

        <div v-if="exportStatus === 'idle'" class="flex items-center gap-3">
          <Button class="h-8 px-2.5" @click="startExport">
            Export All Data
          </Button>
        </div>

        <div v-else-if="exportStatus === 'loading'" class="flex items-center gap-3">
          <div class="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          <span class="text-sm text-muted-foreground">{{ $t('views.AdminOrgSettingsView.exporting_data') }}</span>
        </div>

        <div v-else-if="exportStatus === 'error'" class="flex items-center gap-3">
          <span class="text-sm text-destructive">Export failed: {{ exportError }}</span>
          <button
            type="button"
            class="text-sm font-medium text-primary underline underline-offset-2 hover:no-underline"
            @click="startExport"
          >
            Retry
          </button>
        </div>

        <div v-else-if="exportStatus === 'complete'" class="flex items-center gap-3">
          <span class="badge badge-status-success">{{ $t('views.AdminOrgSettingsView.export_ready') }}</span>
          <span class="text-sm text-muted-foreground">
            Exported at {{ formatDate(exportData.exportedAt) }}
          </span>
          <button
            type="button"
            class="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-input bg-background px-2.5 text-sm font-medium hover:bg-muted transition-all"
            @click="downloadExport"
          >
            Download
          </button>
          <button
            type="button"
            class="text-sm font-medium text-primary underline underline-offset-2 hover:no-underline"
            @click="resetExport"
          >
            Export again
          </button>
        </div>
      </SectionCard>

      <!-- Product Analytics -->
      <ProductAnalyticsSettings />

      <!-- Delete Organization -->
      <SectionCard title="Delete Organisation" description="Permanently delete this organisation and all associated data. This action cannot be undone." class="border-destructive/30" title-class="text-destructive" description-class="text-destructive/80">
        <Button severity="danger" class="h-8 px-2.5" @click="deleteDialogOpen = true">
          Delete Organisation
        </Button>
      </SectionCard>
      </div>
    </template>

    <FormDialog
      :open="deleteDialogOpen"
      @update:open="deleteDialogOpen = false"
      title="Delete Organisation"
      description="Permanently delete this organisation and all associated data. This action cannot be undone."
      confirmText="Permanently Delete"
      :confirmDisabled="confirmName !== orgInfo.name || deleting"
      :loading="deleting"
      @confirm="confirmDelete"
    >
      <p class="text-sm text-muted-foreground">
        This will permanently delete <strong>{{ orgInfo.name }}</strong> and all associated data including runs, pipelines, schemas, connectors, and settings.
        <br /><br />
        <span class="font-semibold text-destructive">{{ $t('views.AdminOrgSettingsView.this_action_cannot_be_undone') }}</span>
      </p>
      <div class="space-y-3">
        <label for="org-delete-confirm-input" class="text-sm text-muted-foreground">
          Type <strong class="text-foreground">{{ orgInfo.name }}</strong> to confirm:
        </label>
        <input
          id="org-delete-confirm-input"
          v-model="confirmName"
          :placeholder="orgInfo.name"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-destructive/50"
          data-testid="org-delete-confirm-input"
        />
        <p v-if="deleteError" class="text-sm text-destructive">{{ deleteError }}</p>
      </div>
    </FormDialog>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import SectionCard from '../components/shared/SectionCard.vue'
import { ref, computed, reactive } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FormDialog from '../components/shared/FormDialog.vue'
import ProductAnalyticsSettings from '../components/product-analytics/ProductAnalyticsSettings.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import { shortId } from '../utils/format'
import { formatApiError } from '../lib/api/formatError'
import { formatDateShort, formatDateFilename } from '../lib/formatDate'

const planStore = usePlanStore()
const router = useRouter()

const { data: orgData, loading, error: loadError, load: loadData } = useDataFetch(
  async () => {
    const [overviewResp, orgResp] = await Promise.all([
      (api as any).GET('/api/v1/admin/billing/overview').catch(() => null),
      (api as any).GET('/api/v1/admin/org').catch(() => null),
    ])
    if (overviewResp.error) return { error: { detail: `Failed to load org info: ${formatApiError(overviewResp.error)}` } }
    if (orgResp.error) return { error: { detail: `Failed to load org info: ${formatApiError(orgResp.error)}` } }
    const overview = overviewResp.data as BillingOverviewResponse
    const orgProfile = orgResp.data as OrgProfileResponse
    return {
      data: {
        id: orgProfile.id ?? '',
        name: orgProfile.name ?? 'Unnamed Org',
        slug: orgProfile.slug ?? '',
        createdAt: orgProfile.created_at ?? '',
        planTier: overview.plan_tier ?? 'community',
        memberCount: overview.total_users ?? 0,
      }
    }
  },
  { initialValue: { id: '', name: '', slug: '', planTier: 'community' as string, createdAt: '', memberCount: 0 } }
)

const orgInfo = computed(() => orgData.value!)

interface OrgProfileResponse {
  id?: string
  name?: string
  slug?: string
  created_at?: string
  logo_url?: string
  plan_id?: string
}

interface BillingOverviewResponse {
  total_users?: number
  total_teams?: number
  total_pipelines?: number
  plan_tier?: string
  plan_id?: string
}

type ExportStatus = 'idle' | 'loading' | 'complete' | 'error'

const exportStatus = ref<ExportStatus>('idle')
const exportData = reactive({
  raw: null as object | null,
  exportedAt: '',
})
const exportError = ref<string | null>(null)

const deleteDialogOpen = ref(false)
const confirmName = ref('')
const deleting = ref(false)
const deleteError = ref<string | null>(null)

function formatDate(dateStr: string): string {
  if (!dateStr) return 'N/A'
  const d = new Date(dateStr)
  return formatDateShort(d)
}

async function startExport() {
  exportStatus.value = 'loading'
  exportError.value = null
  try {
    const resp = await (api as any).GET('/api/v1/admin/org/export')
    if (resp.error) {
      exportStatus.value = 'error'
      exportError.value = String(resp.error)
      return
    }
    const data = resp.data as { exported_at?: string }
    exportData.raw = data
    exportData.exportedAt = data.exported_at ?? new Date().toISOString()
    exportStatus.value = 'complete'
  } catch (e: unknown) {
    exportStatus.value = 'error'
    exportError.value = formatApiError(e)
  }
}

function downloadExport() {
  if (!exportData.raw) return
  const blob = new Blob([JSON.stringify(exportData.raw, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `org-export-${orgInfo.value.slug || orgInfo.value.id}-${formatDateFilename(new Date())}.json`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

function resetExport() {
  exportStatus.value = 'idle'
  exportData.raw = null
  exportData.exportedAt = ''
}

async function confirmDelete() {
  if (confirmName.value !== orgInfo.value.name) return
  deleting.value = true
  deleteError.value = null
  try {
    const resp = await (api as any).DELETE('/api/v1/admin/org')
    if (resp.error) {
      deleteError.value = `Failed to delete org: ${formatApiError(resp.error)}`
      deleting.value = false
      return
    }
    deleteDialogOpen.value = false
    router.push('/login')
  } catch (e: unknown) {
    deleteError.value = `Failed to delete org: ${formatApiError(e)}`
    deleting.value = false
  }
}

planStore.fetchPlan()
</script>
