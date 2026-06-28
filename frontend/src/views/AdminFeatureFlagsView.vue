<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Feature Flags</h1>
      <p class="mt-1 text-muted-foreground">All known feature flags and their current activation status</p>
      <div v-if="planStore.isLoading" class="mt-2 flex items-center gap-2 text-sm text-muted-foreground">
        <div class="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        Loading plan info...
      </div>
      <div v-else class="mt-2 flex flex-wrap gap-4 text-sm text-muted-foreground">
        <span>
          Plan: <strong class="text-foreground">{{ planStore.currentTier }}</strong>
        </span>
        <span>
          Features enabled:
          <strong class="text-foreground">{{ enabledCount }}</strong>
          /
          <span>{{ planStore.features ? Object.keys(planStore.features).length : 0 }}</span>
        </span>
        <span v-if="planStore.isEnterprise" class="font-medium badge badge-context-purple">Enterprise tier</span>
      </div>
    </header>

    <div class="card p-4">
      <h2 class="mb-3 text-lg font-semibold">License Status</h2>
      <div v-if="loading" class="flex items-center justify-center py-8">
        <div class="h-6 w-6 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
      <div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div>
          <span class="text-xs font-medium text-muted-foreground">Tier</span>
          <p class="mt-0.5 text-lg font-semibold">{{ license.tier }}</p>
        </div>
        <div>
          <span class="text-xs font-medium text-muted-foreground">License Key</span>
          <p class="mt-0.5">
            <span :class="license.has_license_key ? 'badge badge-status-success' : 'badge badge-status-muted'">
              {{ license.has_license_key ? 'Active' : 'Not set' }}
            </span>
          </p>
        </div>
        <div>
          <span class="text-xs font-medium text-muted-foreground">Status</span>
          <p class="mt-0.5">
            <span :class="license.is_valid ? 'badge badge-status-success' : 'badge badge-status-destructive'">
              {{ license.is_valid ? 'Valid' : 'Invalid' }}
            </span>
          </p>
        </div>
      </div>
    </div>

    <div v-if="wouldActivate.length > 0" class="card p-4 border-warning/30">
      <h2 class="mb-2 text-sm font-semibold text-warning">Would activate with a license key</h2>
      <p class="mb-3 text-sm text-warning/80">
        The following {{ wouldActivate.length }} feature{{ wouldActivate.length === 1 ? '' : 's' }} would become available
        if an enterprise license key were configured.
      </p>
      <div class="flex flex-wrap gap-2">
        <span
          v-for="flag in wouldActivate"
          :key="flag.name"
          class="badge badge-status-warning"
        >
          {{ flag.name }} <span class="opacity-70">({{ flag.tier }})</span>
        </span>
      </div>
    </div>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadFlags" />

    <div v-else class="card overflow-hidden">
      <table class="w-full">
  <thead>
    <tr class="border-b bg-muted/30 text-left text-xs font-medium uppercase text-muted-foreground">
      <th class="px-4 py-3">Flag</th>
      <th class="px-4 py-3">Tier</th>
      <th class="px-4 py-3">Status</th>
      <th class="px-4 py-3">Description</th>
    </tr>
  </thead>
  <tbody class="divide-y divide-border">
    <tr
      v-for="flag in flags"
      :key="flag.name"
      class="transition-colors hover:bg-muted/20"
    >
            <td class="px-4 py-3 font-mono text-sm font-medium">{{ flag.name }}</td>
            <td class="px-4 py-3">
              <span
                class="inline-block rounded-full px-2.5 py-0.5 text-xs font-medium"
                :class="tierBadgeClass(flag.tier)"
              >
                {{ flag.tier }}
              </span>
            </td>
            <td class="px-4 py-3">
              <span :class="flag.currently_active ? 'badge badge-status-success' : 'badge badge-status-muted'">
                {{ flag.currently_active ? 'Active' : 'Inactive' }}
              </span>
            </td>
            <td class="px-4 py-3 text-sm text-muted-foreground">{{ flag.description }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { api } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

const planStore = usePlanStore()

const enabledCount = computed(() => {
  return Object.values(planStore.features).filter(Boolean).length
})

interface FlagItem {
  name: string
  description: string
  tier: string
  currently_active: boolean
  depends_on: string[] | null
}

interface LicenseInfo {
  tier: string
  has_license_key: boolean
  is_valid: boolean
}

interface FlagsResponse {
  license: LicenseInfo
  flags: FlagItem[]
  would_activate: FlagItem[]
}

const flags = ref<FlagItem[]>([])
const license = ref<LicenseInfo>({ tier: 'free', has_license_key: false, is_valid: true })
const wouldActivate = ref<FlagItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

function tierBadgeClass(tier: string): string {
  switch (tier) {
    case 'free': return 'badge badge-status-success'
    case 'enterprise': return 'badge badge-context-purple'
    case 'v1': return 'badge badge-context-teal'
    case 'v2': return 'badge badge-context-blue'
    default: return 'badge badge-context-slate'
  }
}

async function loadFlags() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await (api as any).GET('/api/v1/admin/feature-flags')
    if (err) {
      error.value = `Failed to load feature flags: ${err}`
    } else if (data) {
      const resp = data as FlagsResponse
      flags.value = resp.flags
      license.value = resp.license
      wouldActivate.value = resp.would_activate
    }
  } catch (e: unknown) {
    error.value = `Failed to load feature flags: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  planStore.fetchPlan()
  loadFlags()
})
</script>
