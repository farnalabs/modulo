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
        <span v-if="planStore.isEnterprise" class="font-medium text-purple-600">Enterprise tier</span>
      </div>
    </header>

    <div class="rounded-lg border bg-card p-4 shadow-sm">
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
            <span
              class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
              :class="license.has_license_key ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'"
            >
              <span class="h-1.5 w-1.5 rounded-full" :class="license.has_license_key ? 'bg-green-500' : 'bg-gray-400'" />
              {{ license.has_license_key ? 'Active' : 'Not set' }}
            </span>
          </p>
        </div>
        <div>
          <span class="text-xs font-medium text-muted-foreground">Status</span>
          <p class="mt-0.5">
            <span
              class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
              :class="license.is_valid ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'"
            >
              <span class="h-1.5 w-1.5 rounded-full" :class="license.is_valid ? 'bg-green-500' : 'bg-red-500'" />
              {{ license.is_valid ? 'Valid' : 'Invalid' }}
            </span>
          </p>
        </div>
      </div>
    </div>

    <div v-if="wouldActivate.length > 0" class="rounded-lg border border-amber-200 bg-amber-50 p-4 shadow-sm">
      <h2 class="mb-2 text-sm font-semibold text-amber-800">Would activate with a license key</h2>
      <p class="mb-3 text-sm text-amber-700">
        The following {{ wouldActivate.length }} feature{{ wouldActivate.length === 1 ? '' : 's' }} would become available
        if an enterprise license key were configured.
      </p>
      <div class="flex flex-wrap gap-2">
        <span
          v-for="flag in wouldActivate"
          :key="flag.name"
          class="inline-flex items-center gap-1.5 rounded-full border border-amber-300 bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800"
        >
          {{ flag.name }}
          <span class="text-amber-500">({{ flag.tier }})</span>
        </span>
      </div>
    </div>

    <div v-if="loading" class="flex items-center justify-center py-16">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
      {{ error }}
      <button class="ml-2 underline" @click="loadFlags">Retry</button>
    </div>

    <div v-else class="overflow-hidden rounded-lg border bg-card shadow-sm">
      <table class="w-full">
        <thead>
          <tr class="border-b bg-muted/50 text-left text-xs font-medium uppercase text-muted-foreground">
            <th class="px-4 py-3">Flag</th>
            <th class="px-4 py-3">Tier</th>
            <th class="px-4 py-3">Status</th>
            <th class="px-4 py-3">Description</th>
          </tr>
        </thead>
        <tbody class="divide-y">
          <tr
            v-for="flag in flags"
            :key="flag.name"
            class="transition-colors hover:bg-muted/30"
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
              <span
                class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
                :class="flag.currently_active ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'"
              >
                <span class="h-1.5 w-1.5 rounded-full" :class="flag.currently_active ? 'bg-green-500' : 'bg-gray-400'" />
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
    case 'free': return 'bg-green-100 text-green-700'
    case 'enterprise': return 'bg-purple-100 text-purple-700'
    case 'v1': return 'bg-blue-100 text-blue-700'
    case 'v2': return 'bg-indigo-100 text-indigo-700'
    default: return 'bg-gray-100 text-gray-700'
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
