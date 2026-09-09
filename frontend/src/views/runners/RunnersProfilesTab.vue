<template>
  <div class="space-y-6">
    <LoadingSpinner v-if="store.isLoading" />

    <ErrorAlert v-else-if="store.error" :message="store.error" :on-retry="store.fetchProfiles" />

    <template v-else-if="rows.length === 0">
      <div v-if="search" class="card p-8 text-center">
        <p class="text-lg font-medium">{{ $t('views.RunnersProfilesTab.no_profiles_match', { search }) }}</p>
        <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.RunnersProfilesTab.try_a_different_search_term') }}</p>
      </div>
      <div v-else class="card p-8 text-center">
        <p class="text-lg font-medium">{{ $t('views.RunnersProfilesTab.no_runner_profiles') }}</p>
        <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.RunnersProfilesTab.no_runner_profiles_hint') }}</p>
        <Button class="mt-4" data-testid="envprofile-list-empty-create" @click="$router.push('/admin/runners/profiles/new')">
          {{ $t('views.RunnersProfilesTab.new_profile') }}
        </Button>
      </div>
    </template>

    <template v-else>
      <div class="flex items-center gap-3">
        <div class="relative w-full sm:w-72">
          <input
            v-model="search"
            type="text"
            :placeholder="$t('views.RunnersProfilesTab.search_profiles')"
            :aria-label="$t('views.RunnersProfilesTab.search_profiles')"
            class="pl-9 pr-3 py-1.5 border border-input bg-background rounded-lg text-sm w-full"
            data-testid="envprofile-list-search"
          />
        </div>
        <Button class="ml-auto border-primary/30 hover:border-primary/60" data-testid="envprofile-list-new" @click="$router.push('/admin/runners/profiles/new')">
          {{ $t('views.RunnersProfilesTab.new_profile') }}
        </Button>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="row in filteredRows"
          :key="row.profile.id"
          class="card p-5 flex flex-col gap-3"
          :class="{ 'opacity-60': !row.available }"
        >
          <div class="flex items-start justify-between">
            <div class="min-w-0">
              <span class="font-semibold text-sm block truncate" data-testid="envprofile-list-name">{{ row.profile.name }}</span>
              <p v-if="row.profile.description" class="mt-0.5 text-xs text-muted-foreground line-clamp-2">{{ row.profile.description }}</p>
            </div>
            <span
              class="shrink-0 inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
              :class="healthBadgeClass(row)"
              :data-testid="`runner-profile-health-${row.profile.id}`"
            >
              <span class="h-1.5 w-1.5 rounded-full" :class="healthDotClass(row)" aria-hidden="true" />
              {{ healthLabel(row) }}
            </span>
          </div>

          <div class="flex flex-wrap gap-1.5">
            <span
              class="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
              data-testid="envprofile-list-tier-badge"
            >
              {{ tierLabel(row.profile.provider_type) }}
            </span>
            <span class="rounded-full bg-muted px-2 py-0.5 font-mono text-xs text-muted-foreground">
              {{ row.profile.provider_type }}
            </span>
          </div>

          <div
            v-if="row.drift.drifted"
            class="rounded-lg border border-warning/40 bg-warning/10 p-3"
            data-testid="runner-profile-drift"
          >
            <p class="text-xs font-medium text-warning">{{ $t('views.RunnersProfilesTab.template_updated') }}</p>
            <p class="mt-0.5 text-xs text-muted-foreground">{{ $t('views.RunnersProfilesTab.template_updated_hint') }}</p>
            <Button
              class="mt-2"
              size="small"
              severity="secondary"
              outlined
              data-testid="runners-profiles-apply"
              :loading="applying"
              @click="applyTemplate(row.profile.id)"
            >
              {{ $t('views.RunnersProfilesTab.apply') }}
            </Button>
            <p v-if="applyError" class="mt-2 text-xs text-destructive">{{ applyError }}</p>
          </div>

          <div class="flex items-center gap-2 mt-auto">
            <Button severity="secondary" outlined size="small" data-testid="envprofile-list-edit" @click="$router.push(`/admin/runners/profiles/${row.profile.id}/edit`)">
              {{ $t('views.RunnersProfilesTab.edit') }}
            </Button>
            <Button
              severity="secondary"
              outlined
              size="small"
              data-testid="envprofile-test"
              :disabled="testResult.profileId === row.profile.id && testResult.running"
              @click="testConnection(row.profile)"
            >
              {{ testResult.profileId === row.profile.id && testResult.running ? $t('views.RunnersProfilesTab.testing') : $t('views.RunnersProfilesTab.test_connection') }}
            </Button>
            <button
              v-if="!row.isTemplate"
              type="button"
              class="ml-auto rounded p-1 text-destructive hover:bg-destructive/10 transition-colors"
              data-testid="envprofile-list-delete"
              :aria-label="$t('views.RunnersProfilesTab.delete_profile')"
              @click="confirmDelete(row.profile)"
            >
              <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
              </svg>
            </button>
            <button
              v-else
              type="button"
              class="ml-auto rounded p-1 text-muted-foreground/50 cursor-not-allowed"
              :aria-label="$t('views.RunnersProfilesTab.template_delete_protected')"
              :title="$t('views.RunnersProfilesTab.template_delete_protected')"
              disabled
              data-testid="envprofile-list-delete"
            >
              <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
              </svg>
            </button>
          </div>

          <div v-if="row.isTemplate" class="rounded-lg border border-input bg-muted/30 p-3" data-testid="runner-profile-detail">
            <dl class="grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
              <dt class="text-muted-foreground">{{ $t('views.RunnersProfilesTab.image_ref') }}</dt>
              <dd class="font-mono truncate" :title="row.profile.image_ref ?? ''">{{ row.profile.image_ref }}</dd>
              <dt class="text-muted-foreground">{{ $t('views.RunnersProfilesTab.network_policy') }}</dt>
              <dd>{{ row.health?.network_policy ?? '—' }}</dd>
              <dt class="text-muted-foreground">{{ $t('views.RunnersProfilesTab.persistence_policy') }}</dt>
              <dd>{{ row.health?.persistence_policy ?? '—' }}</dd>
              <dt class="text-muted-foreground">{{ $t('views.RunnersProfilesTab.resource_limits') }}</dt>
              <dd>{{ resourceSummary(row) }}</dd>
              <template v-if="row.probeError">
                <dt class="text-muted-foreground">{{ $t('views.RunnersProfilesTab.last_error') }}</dt>
                <dd class="text-destructive truncate" :title="row.probeError">{{ row.probeError }}</dd>
              </template>
            </dl>
            <p v-if="row.probeError" class="mt-2 text-xs text-muted-foreground">{{ $t('views.RunnersProfilesTab.remediation_hint') }}</p>
            <p class="mt-2 text-xs text-muted-foreground">{{ $t('views.RunnersProfilesTab.operator_guide_ref') }}</p>
          </div>

          <div v-if="testResult.profileId === row.profile.id" class="rounded-lg border border-input bg-muted/30 p-3" data-testid="envprofile-test-panel">
            <div class="flex items-center justify-between mb-2">
              <h3 class="text-xs font-semibold">{{ $t('views.RunnersProfilesTab.test_connection_for', { name: row.profile.name }) }}</h3>
              <button type="button" class="text-xs text-muted-foreground hover:text-foreground" data-testid="envprofile-test-dismiss" @click="closeTestResult">
                {{ $t('views.RunnersProfilesTab.dismiss') }}
              </button>
            </div>
            <div class="space-y-1">
              <div
                v-for="(event, idx) in testResult.events"
                :key="idx"
                class="flex items-center gap-2 text-xs font-mono"
                :class="event.event === 'failed' ? 'text-destructive' : 'text-muted-foreground'"
              >
                <span
                  class="inline-block h-2 w-2 rounded-full shrink-0"
                  aria-hidden="true"
                  :class="{
                    'bg-yellow-400': event.event === 'provisioning' || event.event === 'destroying',
                    'bg-success': event.event === 'provisioned' || event.event === 'destroyed' || event.event === 'command_complete',
                    'bg-destructive': event.event === 'failed',
                    'bg-primary': event.event === 'command_start',
                  }"
                />
                <span>{{ event.event }}</span>
                <span>{{ event.detail }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="deleteConfirmId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
        <p class="text-sm font-medium text-destructive">{{ $t('views.RunnersProfilesTab.delete_confirm', { name: deleteConfirmName }) }}</p>
        <p class="mt-1 text-sm text-destructive/80">{{ $t('views.RunnersProfilesTab.soft_delete_warning') }}</p>
        <div class="mt-3 flex items-center gap-2">
          <Button :disabled="deleting" severity="danger" size="small" data-testid="envprofile-list-delete-confirm" @click="doDelete">
            {{ deleting ? $t('views.RunnersProfilesTab.deleting') : $t('views.RunnersProfilesTab.delete') }}
          </Button>
          <button
            type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="envprofile-list-delete-cancel"
            @click="deleteConfirmId = null"
          >
            {{ $t('views.RunnersProfilesTab.cancel') }}
          </button>
        </div>
        <div v-if="deleteError" class="mt-2 text-sm text-destructive">{{ deleteError }}</div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import LoadingSpinner from '../../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../../components/shared/ErrorAlert.vue'
import { useEnvironmentProfilesStore, type EnvironmentProfileSummary } from '../../stores/environmentProfiles'
import { getAuthHeaders } from '../../lib/api/client'
import { formatApiError } from '../../lib/api/formatError'
import { parseSSEStream } from '../../lib/sse'
import { runnerTierForProvider, runnerTierLabelKey } from '../../lib/runnerTiers'
import { api } from '../../lib/api/client'
import type { ProfileDrift, ProfileHealth, RunnersStatus } from '../../lib/runnersStatus'

const props = defineProps<{
  status: RunnersStatus | null | undefined
  reloadStatus: () => Promise<void>
}>()

const { t } = useI18n()
const store = useEnvironmentProfilesStore()

const search = ref('')
const deleting = ref(false)
const deleteError = ref<string | null>(null)
const deleteConfirmId = ref<string | null>(null)
const deleteConfirmName = ref('')
const applying = ref(false)
const applyError = ref<string | null>(null)

interface TestEvent {
  event: string
  detail: string
  timestamp: string
}

const testResult = reactive<{ profileId: string | null; running: boolean; events: TestEvent[] }>({
  profileId: null,
  running: false,
  events: [],
})

interface ProfileRow {
  profile: EnvironmentProfileSummary
  health: ProfileHealth | null
  isTemplate: boolean
  available: boolean
  drift: ProfileDrift
  probeError: string | null
  configJson: Record<string, unknown>
}

const rows = computed<ProfileRow[]>(() => {
  const byId = new Map((props.status?.profiles ?? []).map((p) => [p.id, p]))
  return store.profiles.map((profile) => {
    const health = byId.get(profile.id) ?? null
    const isTemplate = health?.drift?.is_seeded ?? profile.provider_type === 'runner_docker'
    return {
      profile,
      health,
      isTemplate,
      available: health?.available ?? true,
      drift: health?.drift ?? { is_seeded: false, drifted: false, drifted_fields: [] },
      probeError: props.status?.machines?.find((m) => m.probe_error)?.probe_error ?? null,
      configJson: health?.config_json ?? {},
    }
  })
})

const filteredRows = computed(() => {
  if (!search.value) return rows.value
  const q = search.value.toLowerCase()
  return rows.value.filter(
    (r) =>
      r.profile.name.toLowerCase().includes(q)
      || (r.profile.description ?? '').toLowerCase().includes(q)
      || r.profile.provider_type.toLowerCase().includes(q),
  )
})

function tierLabel(providerType: string): string {
  const tier = runnerTierForProvider(providerType)
  return tier ? t(runnerTierLabelKey(tier)) : providerType
}

function healthLabel(row: ProfileRow): string {
  if (row.profile.provider_type !== 'runner_docker') return row.profile.status === 'active' ? t('views.RunnersProfilesTab.status_active') : row.profile.status
  switch (row.health?.health_state) {
    case 'healthy':
      return t('views.RunnersProfilesTab.health_healthy')
    case 'engine_unreachable':
      return t('views.RunnersProfilesTab.health_unreachable')
    case 'image_not_pulled':
      return t('views.RunnersProfilesTab.health_image_missing')
    default:
      return t('views.RunnersProfilesTab.health_unknown')
  }
}

function healthBadgeClass(row: ProfileRow): string {
  if (row.profile.provider_type !== 'runner_docker') return 'bg-success/10 text-success'
  switch (row.health?.health_state) {
    case 'healthy':
      return 'bg-success/10 text-success'
    case 'engine_unreachable':
      return 'bg-destructive/10 text-destructive'
    case 'image_not_pulled':
      return 'bg-warning/10 text-warning'
    default:
      return 'bg-muted text-muted-foreground'
  }
}

function healthDotClass(row: ProfileRow): string {
  if (row.profile.provider_type !== 'runner_docker') return 'bg-success'
  switch (row.health?.health_state) {
    case 'healthy':
      return 'bg-success'
    case 'engine_unreachable':
      return 'bg-destructive'
    case 'image_not_pulled':
      return 'bg-warning'
    default:
      return 'bg-muted-foreground'
  }
}

function resourceSummary(row: ProfileRow): string {
  const memoryMb = typeof row.configJson['memory_mb'] === 'number' ? (row.configJson['memory_mb'] as number) : null
  const cpuLimit = typeof row.configJson['cpu_limit'] === 'number' ? (row.configJson['cpu_limit'] as number) : null
  if (memoryMb === null && cpuLimit === null) {
    return t('views.RunnersProfilesTab.limits_unset')
  }
  return t('views.RunnersProfilesTab.limits_summary', {
    cpu: cpuLimit ?? t('views.RunnersProfilesTab.limits_unset_value'),
    mem: memoryMb ?? t('views.RunnersProfilesTab.limits_unset_value'),
  })
}

async function applyTemplate(profileId: string) {
  applying.value = true
  applyError.value = null
  try {
    const { error } = await api.POST('/api/v1/runners/profiles/{profile_id}/apply-template', {
      params: { path: { profile_id: profileId } },
    })
    if (error) {
      applyError.value = formatApiError(error)
    } else {
      await Promise.all([store.fetchProfiles(), props.reloadStatus()])
    }
  } catch (e: unknown) {
    applyError.value = formatApiError(e)
  } finally {
    applying.value = false
  }
}

function confirmDelete(profile: EnvironmentProfileSummary) {
  deleteConfirmId.value = profile.id
  deleteConfirmName.value = profile.name
  deleteError.value = null
}

async function doDelete() {
  if (!deleteConfirmId.value) return
  deleting.value = true
  deleteError.value = null
  try {
    await store.deleteProfile(deleteConfirmId.value)
    deleteConfirmId.value = null
  } catch (e: unknown) {
    deleteError.value = e instanceof Error ? e.message : t('views.RunnersProfilesTab.delete_failed')
  } finally {
    deleting.value = false
  }
}

async function testConnection(profile: EnvironmentProfileSummary) {
  testResult.profileId = profile.id
  testResult.running = true
  testResult.events = []
  try {
    const response = await fetch(`/api/v1/environment-profiles/${profile.id}/test`, {
      method: 'POST',
      headers: getAuthHeaders(),
    })
    if (!response.ok) {
      testResult.events.push({ event: 'failed', detail: `HTTP ${response.status}`, timestamp: new Date().toISOString() })
      return
    }
    const reader = response.body?.getReader()
    if (!reader) {
      testResult.events.push({ event: 'failed', detail: t('views.RunnersProfilesTab.no_response_body'), timestamp: new Date().toISOString() })
      return
    }
    for await (const message of parseSSEStream(reader)) {
      if (!message.data) continue
      try {
        testResult.events.push(JSON.parse(message.data) as TestEvent)
      } catch {
        testResult.events.push({ event: 'info', detail: message.data, timestamp: new Date().toISOString() })
      }
    }
  } catch (e: unknown) {
    testResult.events.push({ event: 'failed', detail: formatApiError(e), timestamp: new Date().toISOString() })
  } finally {
    testResult.running = false
  }
}

function closeTestResult() {
  testResult.profileId = null
  testResult.running = false
  testResult.events = []
}

onMounted(() => {
  if (store.profiles.length === 0) {
    void store.fetchProfiles()
  }
})
</script>
