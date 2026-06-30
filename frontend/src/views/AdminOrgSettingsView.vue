<template>
  <FeatureGate feature-name="team_rbac" required-tier="enterprise">
    <template #locked="{ tooltip }">
      <div data-theme="agent" class="mx-auto max-w-6xl space-y-6 p-6">
        <div class="mb-4 flex items-center gap-2 rounded-lg border border-warning/30 bg-warning/5 p-4 text-sm text-warning">
          <LockIcon :locked="true" :tooltip="tooltip" />
          <span>Team RBAC is not available on your current plan.</span>
        </div>
      </div>
    </template>

    <div data-theme="agent" class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Organisation Settings</h1>
      <p class="mt-1 text-muted-foreground">Manage your organisation profile, export data, or delete the organisation</p>
    </header>

    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

    <template v-else>
      <!-- Org Info -->
      <div class="card p-4">
        <h2 class="mb-3 text-lg font-semibold">Organisation Info</h2>
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div>
            <span class="text-xs font-medium text-muted-foreground">Name</span>
            <p class="mt-0.5 text-lg font-semibold">{{ orgInfo.name }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">Slug</span>
            <p class="mt-0.5 font-mono text-sm">{{ orgInfo.slug }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">Plan</span>
            <p class="mt-0.5">
              <span :class="orgInfo.planTier === 'enterprise' ? 'badge badge-context-purple' : 'badge badge-status-muted'">
                {{ orgInfo.planTier === 'enterprise' ? 'Enterprise' : 'Free' }}
              </span>
            </p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">Created</span>
            <p class="mt-0.5 text-sm font-medium">{{ formatDate(orgInfo.createdAt) }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">Members</span>
            <p class="mt-0.5 text-lg font-semibold">{{ orgInfo.memberCount }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">Org ID</span>
            <p class="mt-0.5 font-mono text-xs text-muted-foreground">{{ orgInfo.id }}</p>
          </div>
        </div>
      </div>

      <!-- Data Export -->
      <div class="card p-4">
        <h2 class="mb-3 text-lg font-semibold">Data Export</h2>
        <p class="mb-4 text-sm text-muted-foreground">
          Export all organisation data including runs, pipelines, schemas, connectors, and settings.
        </p>

        <div v-if="exportStatus === 'idle'" class="flex items-center gap-3">
          <button
            class="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-transparent bg-primary px-2.5 text-sm font-medium text-primary-foreground transition-all hover:bg-primary/80"
            @click="startExport"
          >
            Export All Data
          </button>
        </div>

        <div v-else-if="exportStatus === 'loading'" class="flex items-center gap-3">
          <div class="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          <span class="text-sm text-muted-foreground">Exporting data...</span>
        </div>

        <div v-else-if="exportStatus === 'error'" class="flex items-center gap-3">
          <span class="text-sm text-destructive">Export failed: {{ exportError }}</span>
          <button
            class="text-sm font-medium text-primary underline underline-offset-2 hover:no-underline"
            @click="startExport"
          >
            Retry
          </button>
        </div>

        <div v-else-if="exportStatus === 'complete'" class="flex items-center gap-3">
          <span class="badge badge-status-success">Export ready</span>
          <span class="text-sm text-muted-foreground">
            Exported at {{ formatDate(exportData.exportedAt) }}
          </span>
          <button
            class="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-input bg-background px-2.5 text-sm font-medium hover:bg-muted transition-all"
            @click="downloadExport"
          >
            Download
          </button>
          <button
            class="text-sm font-medium text-primary underline underline-offset-2 hover:no-underline"
            @click="resetExport"
          >
            Export again
          </button>
        </div>
      </div>

      <!-- Delete Organization -->
      <div class="card border-destructive/30 p-4">
        <h2 class="mb-3 text-lg font-semibold text-destructive">Delete Organisation</h2>
        <p class="mb-4 text-sm text-destructive/80">
          Permanently delete this organisation and all associated data. This action cannot be undone.
        </p>
        <button
          class="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-transparent bg-destructive px-2.5 text-sm font-medium text-destructive-foreground transition-all hover:brightness-110"
          @click="deleteDialogOpen = true"
        >
          Delete Organisation
        </button>
      </div>
    </template>

    <!-- Delete Confirmation Dialog -->
    <Dialog :open="deleteDialogOpen" @update:open="deleteDialogOpen = false">
      <DialogContent class="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Delete Organisation</DialogTitle>
          <DialogDescription>
            This will permanently delete <strong>{{ orgInfo.name }}</strong> and all associated data including runs, pipelines, schemas, connectors, and settings.
            <br /><br />
            <span class="font-semibold text-destructive">This action cannot be undone.</span>
          </DialogDescription>
        </DialogHeader>

        <div class="space-y-3">
          <p class="text-sm text-muted-foreground">
            Type <strong class="text-foreground">{{ orgInfo.name }}</strong> to confirm:
          </p>
          <input
            v-model="confirmName"
            :placeholder="orgInfo.name"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-destructive/50"
            data-testid="org-delete-confirm-input"
          />
          <p v-if="deleteError" class="text-sm text-destructive">{{ deleteError }}</p>
        </div>

        <DialogFooter class="gap-2 sm:justify-end">
          <button
            type="button"
            class="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-input bg-background px-2.5 text-sm font-medium hover:bg-muted transition-all"
            @click="cancelDelete"
          >
            Cancel
          </button>
          <button
            type="button"
            :disabled="confirmName !== orgInfo.name || deleting"
            class="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-transparent bg-destructive px-2.5 text-sm font-medium text-destructive-foreground transition-all hover:brightness-110 disabled:opacity-50"
            data-testid="org-delete-confirm-button"
            @click="confirmDelete"
          >
            {{ deleting ? 'Deleting...' : 'Permanently Delete' }}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../components/ui/dialog'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LockIcon from '../components/LockIcon.vue'

const planStore = usePlanStore()
const router = useRouter()

const loading = ref(true)
const loadError = ref<string | null>(null)

const orgInfo = reactive({
  id: '',
  name: '',
  slug: '',
  planTier: 'free' as string,
  createdAt: '',
  memberCount: 0,
})

interface ExportResponse {
  organisation?: {
    id?: string
    name?: string
    slug?: string
    created_at?: string
  }
  exported_at?: string
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
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })
}

async function loadData() {
  loading.value = true
  loadError.value = null
  try {
    const [overviewResp, exportResp] = await Promise.all([
      (api as any).GET('/api/v1/admin/billing/overview'),
      (api as any).GET('/api/v1/admin/org/export'),
    ])

    if (overviewResp.error) {
      loadError.value = `Failed to load org info: ${overviewResp.error}`
      return
    }
    if (exportResp.error) {
      loadError.value = `Failed to load org info: ${exportResp.error}`
      return
    }

    const overview = overviewResp.data as BillingOverviewResponse
    const exportResult = exportResp.data as ExportResponse

    orgInfo.id = exportResult.organisation?.id ?? ''
    orgInfo.name = exportResult.organisation?.name ?? 'Unnamed Org'
    orgInfo.slug = exportResult.organisation?.slug ?? ''
    orgInfo.createdAt = exportResult.organisation?.created_at ?? ''
    orgInfo.planTier = overview.plan_tier ?? 'free'
    orgInfo.memberCount = overview.total_users ?? 0
  } catch (e: unknown) {
    loadError.value = `Failed to load org info: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
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
    const data = resp.data as ExportResponse
    exportData.raw = data
    exportData.exportedAt = data.exported_at ?? new Date().toISOString()
    exportStatus.value = 'complete'
  } catch (e: unknown) {
    exportStatus.value = 'error'
    exportError.value = e instanceof Error ? e.message : String(e)
  }
}

function downloadExport() {
  if (!exportData.raw) return
  const blob = new Blob([JSON.stringify(exportData.raw, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `org-export-${orgInfo.slug || orgInfo.id}-${new Date().toISOString().slice(0, 10)}.json`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function resetExport() {
  exportStatus.value = 'idle'
  exportData.raw = null
  exportData.exportedAt = ''
}

function cancelDelete() {
  deleteDialogOpen.value = false
  confirmName.value = ''
  deleteError.value = null
}

async function confirmDelete() {
  if (confirmName.value !== orgInfo.name) return
  deleting.value = true
  deleteError.value = null
  try {
    const resp = await (api as any).DELETE('/api/v1/admin/org')
    if (resp.error) {
      deleteError.value = `Failed to delete org: ${resp.error}`
      deleting.value = false
      return
    }
    deleteDialogOpen.value = false
    router.push('/login')
  } catch (e: unknown) {
    deleteError.value = `Failed to delete org: ${e instanceof Error ? e.message : String(e)}`
    deleting.value = false
  }
}

onMounted(() => { planStore.fetchPlan(); loadData() })
</script>
