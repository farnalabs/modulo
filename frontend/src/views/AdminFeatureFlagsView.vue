<template>
  <div data-theme="agent" class="mx-auto max-w-6xl space-y-6 p-6">
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
          <span>{{ allFlagsCount }}</span>
        </span>
        <span v-if="planStore.isEnterprise" class="font-medium badge badge-context-purple">Enterprise tier</span>
      </div>
    </header>

    <div class="card p-4">
      <h2 class="mb-3 text-lg font-semibold">License Status</h2>
      <div v-if="loading" class="flex items-center justify-center py-8">
        <div class="h-6 w-6 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
      <div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-4">
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
        <div>
          <span class="text-xs font-medium text-muted-foreground">Expires</span>
          <p class="mt-0.5 text-sm font-medium">
            <template v-if="planStore.expiresAt">
              {{ formatDate(planStore.expiresAt) }}
            </template>
            <span v-else class="badge badge-status-muted">N/A</span>
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

    <div>
      <div class="relative mb-4">
        <Input
          v-model="searchQuery"
          placeholder="Search flags by name or description..." data-testid="search-input"
        />
      </div>

      <LoadingSpinner v-if="loading" />
      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadFlags" />
      <template v-else>
        <TooltipProvider>
          <div
            v-for="section in groupedFlags"
            :key="section.tier"
            class="card mb-6 overflow-hidden"
          >
            <div class="border-b bg-muted/30 px-4 py-2">
              <h3 class="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
                {{ section.label }}
                <span class="ml-2 text-xs font-normal opacity-60">({{ section.flags.length }})</span>
              </h3>
            </div>
            <table class="w-full" v-if="section.flags.length > 0">
              <thead>
                <tr class="border-b bg-muted/10 text-left text-xs font-medium uppercase text-muted-foreground">
                  <th class="px-4 py-3 w-12"></th>
                  <th class="px-4 py-3">Flag</th>
                  <th class="px-4 py-3">Status</th>
                  <th class="px-4 py-3">Description</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-border">
                <tr
                  v-for="flag in section.flags"
                  :key="flag.name"
                  class="transition-colors hover:bg-muted/20"
                >
                  <td class="px-4 py-3">
                    <span
                      class="inline-flex h-5 w-9 shrink-0 cursor-default items-center rounded-full transition-colors"
                      :class="flag.currently_active ? 'bg-primary' : 'bg-input'"
                    >
                      <span
                        class="inline-block h-4 w-4 rounded-full bg-background shadow-sm transition-transform"
                        :class="flag.currently_active ? 'translate-x-[18px]' : 'translate-x-0.5'"
                      />
                    </span>
                  </td>
                  <td class="px-4 py-3">
                    <Tooltip :delay-duration="300">
                      <TooltipTrigger as-child>
                        <span class="font-mono text-sm font-medium cursor-help underline decoration-dotted decoration-muted-foreground/40 underline-offset-2">
                          {{ flag.name }}
                        </span>
                      </TooltipTrigger>
                      <TooltipContent side="top" class="max-w-xs">
                        <p>{{ flag.description }}</p>
                        <p v-if="flag.depends_on" class="mt-1 text-xs opacity-70">
                          Depends on: {{ flag.depends_on.join(', ') }}
                        </p>
                      </TooltipContent>
                    </Tooltip>
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
            <div v-else class="px-4 py-6 text-center text-sm text-muted-foreground">
              No flags match your search in this tier.
            </div>
          </div>
        </TooltipProvider>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { api } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Input from '../components/ui/input/Input.vue'
import {
  TooltipProvider,
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from '../components/ui/tooltip'

const planStore = usePlanStore()

const enabledCount = computed(() => {
  return Object.values(planStore.features).filter(Boolean).length
})

const allFlagsCount = computed(() => flags.value.length)

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

interface FlagGroup {
  tier: string
  label: string
  flags: FlagItem[]
}

const flags = ref<FlagItem[]>([])
const license = ref<LicenseInfo>({ tier: 'free', has_license_key: false, is_valid: true })
const wouldActivate = ref<FlagItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const searchQuery = ref('')

const tierSections: Record<string, string> = {
  free: 'Free',
  enterprise: 'Enterprise',
}

const groupedFlags = computed(() => {
  const query = searchQuery.value.toLowerCase().trim()
  const filtered = query
    ? flags.value.filter(f =>
        f.name.toLowerCase().includes(query) ||
        f.description.toLowerCase().includes(query)
      )
    : flags.value

  const groups: FlagGroup[] = []
  const added = new Set<string>()

  for (const flag of filtered) {
    const tier = flag.tier
    if (!added.has(tier)) {
      added.add(tier)
      groups.push({
        tier,
        label: tierSections[tier] ?? tier.charAt(0).toUpperCase() + tier.slice(1),
        flags: [],
      })
    }
    const group = groups.find(g => g.tier === tier)
    if (group) group.flags.push(flag)
  }

  groups.sort((a, b) => {
    const order = ['free', 'enterprise']
    return order.indexOf(a.tier) - order.indexOf(b.tier)
  })

  return groups
})

function tierBadgeClass(tier: string): string {
  switch (tier) {
    case 'free': return 'badge badge-status-success'
    case 'enterprise': return 'badge badge-context-purple'
    case 'v1': return 'badge badge-context-teal'
    case 'v2': return 'badge badge-context-blue'
    default: return 'badge badge-context-slate'
  }
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })
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
      license.value = resp.license ?? { tier: 'free', has_license_key: false, is_valid: true }
      wouldActivate.value = resp.would_activate ?? []
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
