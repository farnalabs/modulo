<template>
  <div data-theme="agent" class="page-wide">
    <header>
      <PageHeader :title="$t('views.AdminFeatureFlagsView.feature_flags')" :subtitle="$t('views.AdminFeatureFlagsView.all_known_feature_flags_and_their_current_activation_status')" />
      <div v-if="planStore.isLoading" class="mt-2 flex items-center gap-2 text-sm text-muted-foreground">
        <div class="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        {{ $t('views.AdminFeatureFlagsView.loading_plan_info') }}
      </div>
      <div v-else class="mt-2 flex flex-wrap gap-4 text-sm text-muted-foreground">
        <span>
          {{ $t('views.AdminFeatureFlagsView.plan') }}: <strong class="text-foreground">{{ planStore.currentTier }}</strong>
        </span>
        <span>
          {{ $t('views.AdminFeatureFlagsView.features_enabled') }}:
          <strong class="text-foreground">{{ enabledCount }}</strong>
          /
          <span>{{ allFlagsCount }}</span>
        </span>
        <span v-if="planStore.isTeam" class="font-medium badge badge-context-purple">{{ $t('views.AdminFeatureFlagsView.team_tier') }}</span>
      </div>
    </header>

    <div class="card p-4">
      <h2 class="mb-3 text-base font-semibold">{{ $t('views.AdminFeatureFlagsView.license_status') }}</h2>
      <div v-if="loading" class="flex items-center justify-center py-8">
        <div class="h-6 w-6 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
      <div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-4">
        <div>
          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminFeatureFlagsView.tier') }}</span>
          <p class="mt-0.5 text-lg font-semibold">{{ license.tier }}</p>
        </div>
        <div>
          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminFeatureFlagsView.license_key') }}</span>
          <p class="mt-0.5">
            <span :class="license.has_license_key ? 'badge badge-status-success' : 'badge badge-status-muted'">
              {{ license.has_license_key ? $t('views.AdminFeatureFlagsView.active') : $t('views.AdminFeatureFlagsView.not_set') }}
            </span>
          </p>
        </div>
        <div>
          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminFeatureFlagsView.status_label') }}</span>
          <p class="mt-0.5">
            <span :class="license.is_valid ? 'badge badge-status-success' : 'badge badge-status-destructive'">
              {{ license.is_valid ? $t('views.AdminFeatureFlagsView.valid') : $t('views.AdminFeatureFlagsView.invalid') }}
            </span>
          </p>
        </div>
        <div>
          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminFeatureFlagsView.expires') }}</span>
          <p class="mt-0.5 text-sm font-medium">
            <template v-if="planStore.expiresAt">
              {{ formatDate(planStore.expiresAt) }}
            </template>
            <span v-else class="badge badge-status-muted">N/A</span>
          </p>
        </div>
      </div>
    </div>

    <div v-if="filteredWouldActivate.length > 0" class="card p-4 border-warning/30">
      <h2 class="mb-2 text-sm font-semibold text-warning">{{ $t('views.AdminFeatureFlagsView.would_activate') }}</h2>
      <p class="mb-3 text-sm text-warning/80">
        {{ $t('views.AdminFeatureFlagsView.would_activate_features', { count: filteredWouldActivate.length }) }}
      </p>
      <div class="flex flex-wrap gap-2">
        <span
              v-for="flag in filteredWouldActivate"
          :key="flag.name"
          class="badge badge-status-warning"
        >
          {{ flag.name }} <span class="opacity-70">({{ flag.tier }})</span>
        </span>
      </div>
    </div>

    <div>
      <FilterBar
        :search="{ placeholder: 'Search flags by name or description...' }"
        :search-value="searchQuery"
        data-testid="search-input"
        @update:search="searchQuery = $event; currentPage = 1"
      />

      <LoadingSpinner v-if="loading" />
      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadFlags" />
      <template v-else>
        <div v-if="!hasResults && searchQuery" class="flex flex-col items-center justify-center py-16">
          <p class="text-lg font-medium text-muted-foreground">{{ $t('views.AdminFeatureFlagsView.no_results') }}</p>
        </div>
        <template v-else>
          <TooltipProvider>
            <div
              v-for="section in paginatedGroups"
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
                  <tr>
                    <th class="table-header w-12"></th>
                    <th class="table-header">{{ $t('views.AdminFeatureFlagsView.flag') }}</th>
                    <th class="table-header">{{ $t('views.AdminFeatureFlagsView.status') }}</th>
                    <th class="table-header">{{ $t('views.AdminFeatureFlagsView.description') }}</th>
                    <th class="table-header w-32 table-cell-numeric">{{ $t('views.AdminFeatureFlagsView.org_override') }}</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-border">
                  <tr
                    v-for="flag in section.flags"
                    :key="flag.name"
                    class="transition-colors hover:bg-muted/20"
                  >
                    <td class="table-cell">
                      <span
                        class="inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full transition-colors"
                        role="switch"
                        :aria-checked="flag.currently_active"
                        :aria-label="'Toggle ' + flag.name"
                        tabindex="0"
                        @click.stop="toggleFlag(flag)"
                        @keydown.enter="toggleFlag(flag)"
                        @keydown.space.prevent="toggleFlag(flag)"
                        :class="flagToggling[flag.name] ? 'bg-muted-foreground/50' : (flag.currently_active ? 'bg-primary' : 'bg-input')"
                      >
                        <span
                          class="inline-block h-4 w-4 rounded-full bg-background shadow-sm transition-transform"
                          :class="flag.currently_active ? 'translate-x-[18px]' : 'translate-x-0.5'"
                        />
                      </span>
                    </td>
                    <td class="table-cell">
                      <Tooltip :delay-duration="300">
                        <TooltipTrigger as-child>
                          <span class="font-mono text-sm font-medium cursor-help underline decoration-dotted decoration-muted-foreground/40 underline-offset-2">
                            {{ flag.name }}
                          </span>
                        </TooltipTrigger>
                        <TooltipContent side="top" class="max-w-xs">
                          <p>{{ flag.description }}</p>
                          <p v-if="flag.depends_on" class="text-muted-foreground text-[10px]">
                            Depends on {{ flag.depends_on.join(', ') }}
                          </p>
                        </TooltipContent>
                      </Tooltip>
                    </td>
                    <td class="table-cell">
                      <span :class="flag.currently_active ? 'badge badge-status-success' : 'badge badge-status-muted'">
                        {{ flag.currently_active ? $t('views.AdminFeatureFlagsView.active') : $t('views.AdminFeatureFlagsView.inactive') }}
                      </span>
                    </td>
                    <td class="table-cell text-muted-foreground">{{ flag.description }}</td>
                    <td class="table-cell-numeric">
                      <Button variant="outline" size="sm" @click.stop="openOverrideDialog(flag)">
                        {{ getCurrentOverride(flag.name) === null ? $t('views.AdminFeatureFlagsView.default') : (getCurrentOverride(flag.name) ? $t('common.enabled') : $t('common.disabled')) }}
                      </Button>
                    </td>
                  </tr>
                </tbody>
              </table>
              <div v-else class="px-4 py-6 text-center text-sm text-muted-foreground">
                No flags in this tier.
              </div>
            </div>
          </TooltipProvider>
          <div v-if="totalPages > 1" class="flex items-center justify-center gap-4 py-4">
            <span class="text-sm text-muted-foreground">{{ $t('views.AdminFeatureFlagsView.page_of', { current: currentPage, total: totalPages }) }}</span>
            <div class="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                :disabled="currentPage <= 1"
                @click="currentPage = Math.max(1, currentPage - 1)"
              >
                {{ $t('common.previous') }}
              </Button>
              <Button
                variant="outline"
                size="sm"
                :disabled="currentPage >= totalPages"
                @click="currentPage = Math.min(totalPages, currentPage + 1)"
              >
                {{ $t('common.next') }}
              </Button>
            </div>
          </div>
        </template>
      </template>
    </div>
    <FormDialog
      :open="overrideDialogOpen"
      @update:open="overrideDialogOpen = !!$event"
      title="Org Override"
      :description="overrideDescription"
      confirm-text="Save"
      @confirm="saveOverride"
    >
      <div class="py-4">
        <Select aria-label="Form control" v-model="overrideDialogValue">
          <SelectTrigger>
            <SelectValue :placeholder="$t('views.AdminFeatureFlagsView.select_override')" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="null">{{ $t('views.AdminFeatureFlagsView.system_default') }}</SelectItem>
            <SelectItem value="true">{{ $t('views.AdminFeatureFlagsView.force_enabled') }}</SelectItem>
            <SelectItem value="false">{{ $t('views.AdminFeatureFlagsView.force_disabled') }}</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </FormDialog>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { usePlanStore } from '../stores/planStore'
import { formatApiError } from '../lib/api/formatError'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Button } from '../components/ui/button'
import FormDialog from '../components/shared/FormDialog.vue'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../components/ui/select'
import {
  TooltipProvider,
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from '../components/ui/tooltip'
import { formatDateShort } from '../lib/formatDate'

const planStore = usePlanStore()
const { t } = useI18n()

const enabledCount = computed(() => {
  return Object.values(planStore.features).filter(Boolean).length
})

const allFlagsCount = computed(() => (flags.value ?? []).length)

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

const searchQuery = ref('')
const currentPage = ref(1)
const pageSize = 10

const { data: flagsResponse, loading, error, load: loadFlags } = useDataFetch(
  () => (api as any).GET('/api/v1/admin/feature-flags') as Promise<{ data?: FlagsResponse; error?: { detail?: string } }>,
  { initialValue: { flags: [] as FlagItem[], license: { tier: 'community', has_license_key: false, is_valid: true } as LicenseInfo, would_activate: [] as FlagItem[] } as FlagsResponse }
)

const flags = computed(() => flagsResponse.value?.flags ?? [])
const license = computed(() => flagsResponse.value?.license ?? { tier: 'community', has_license_key: false, is_valid: true })
const wouldActivate = computed(() => flagsResponse.value?.would_activate ?? [])

const filteredFlags = computed(() => {
  const query = searchQuery.value.toLowerCase().trim()
  const items = flags.value
  return query
    ? items.filter(f =>
        f.name.toLowerCase().includes(query) ||
        f.description.toLowerCase().includes(query)
      )
    : items
})

const totalPages = computed(() => Math.max(1, Math.ceil(filteredFlags.value.length / pageSize)))

const paginatedGroups = computed(() => {
  const start = (currentPage.value - 1) * pageSize
  const page = filteredFlags.value.slice(start, start + pageSize)

  const groups: FlagGroup[] = []
  const added = new Set<string>()

  for (const flag of page) {
    const tier = flag.tier
    if (!added.has(tier)) {
      added.add(tier)
      groups.push({
        tier,
        label: planStore.getTierLabel(tier),
        flags: [],
      })
    }
    const group = groups.find(g => g.tier === tier)
    if (group) group.flags.push(flag)
  }

  groups.sort((a, b) => {
    const order = ['community', 'team']
    return order.indexOf(a.tier) - order.indexOf(b.tier)
  })

  return groups
})

const filteredWouldActivate = computed(() => {
  const query = searchQuery.value.toLowerCase().trim()
  const items = wouldActivate.value
  if (!query) return items
  return items.filter(f =>
    f.name.toLowerCase().includes(query) ||
    f.description.toLowerCase().includes(query)
  )
})

const hasResults = computed(() => filteredFlags.value.length > 0)

watch(searchQuery, () => {
  currentPage.value = 1
})

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  return formatDateShort(d)
}

watch(() => flagsResponse.value?.flags, (newFlags) => {
  if (newFlags) {
    for (const flag of newFlags) {
      planStore.fetchOrgFlagOverride(flag.name).then(override => {
        planStore.orgOverrides[flag.name] = override
      })
    }
  }
})

const flagToggling = ref<Record<string, boolean>>({})

const overrideDialogFlag = ref<FlagItem | null>(null)
const overrideDialogOpen = ref(false)
const overrideDialogValue = ref<string>('null')

const overrideDescription = computed(() =>
  overrideDialogFlag.value
    ? `${t('views.AdminFeatureFlagsView.org_override_for')} "${overrideDialogFlag.value.name}"`
    : ''
)

function openOverrideDialog(flag: FlagItem) {
  const current = planStore.orgOverrides[flag.name]
  overrideDialogFlag.value = flag
  overrideDialogValue.value = current === true ? 'true' : current === false ? 'false' : 'null'
  overrideDialogOpen.value = true
}

function getCurrentOverride(flagName: string): boolean | null {
  return planStore.orgOverrides[flagName] ?? null
}

async function saveOverride() {
  const flag = overrideDialogFlag.value
  if (!flag) return
  const val = overrideDialogValue.value
  const enabled = val === 'null' ? null : val === 'true'
  await planStore.setOrgFlagOverride(flag.name, enabled)
  overrideDialogOpen.value = false
  overrideDialogFlag.value = null
}

async function toggleFlag(flag: FlagItem) {
  flagToggling.value[flag.name] = true
  const enabled = !flag.currently_active
  const { error: err } = await (api as any).PUT('/api/v1/admin/feature-flags/{flag_name}', {
    params: { path: { flag_name: flag.name } },
    body: { enabled },
  })
  if (err) {
    error.value = `Failed to toggle flag: ${formatApiError(err)}`
    flagToggling.value[flag.name] = false
    return
  }
  await loadFlags()
  flagToggling.value[flag.name] = false
}

planStore.fetchPlan()
</script>
