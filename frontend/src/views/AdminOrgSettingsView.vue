<template>
  <div data-theme="agent" class="page-wide">
    <PageHeader
      :title="$t('views.AdminOrgSettingsView.title')"
      :subtitle="$t('views.AdminOrgSettingsView.subtitle')"
    />

    <!-- HITL review window (FAR-1257): the ORG default, the middle layer of
         pipeline override > org default > instance default. Value + unit
         select for readability; the API contract is seconds. Empty value on
         save sends null, which clears the org key (inherit instance).
         FAR-1269: deliberately OUTSIDE the team-tier FeatureGate below —
         GET/PUT /api/v1/admin/org/hitl-review-window is not plan-gated, so
         this control must render and work on every plan tier. Every other
         section stays inside the gate. -->
    <SectionCard
      :title="$t('views.AdminOrgSettingsView.hitl_review_window')"
      :description="$t('views.AdminOrgSettingsView.hitl_review_window_description')"
    >
      <div v-if="hitlWindowLoading" class="space-y-2" aria-hidden="true" data-testid="org-hitl-review-window-skeleton">
        <div class="h-4 w-32 animate-pulse rounded bg-muted" />
        <div class="h-10 w-full animate-pulse rounded bg-muted" />
      </div>

      <div v-else-if="hitlWindowLoadError" class="space-y-2">
        <p class="text-sm text-destructive" role="alert" data-testid="org-hitl-review-window-load-error">{{ hitlWindowLoadError }}</p>
        <button
          type="button"
          class="text-sm font-medium text-primary-text underline underline-offset-2 hover:no-underline"
          data-testid="org-hitl-review-window-retry"
          @click="loadHitlReviewWindow"
        >
          {{ $t('views.AdminOrgSettingsView.hitl_review_window_retry') }}
        </button>
      </div>

      <template v-else>
        <div class="flex flex-wrap items-end gap-3">
          <div class="w-32">
            <label for="org-hitl-review-window-value" class="mb-1.5 block text-xs font-medium text-muted-foreground">
              {{ $t('views.AdminOrgSettingsView.hitl_review_window_value') }}
            </label>
            <input
              id="org-hitl-review-window-value"
              v-model="hitlWindowValue"
              type="number"
              min="1"
              step="1"
              inputmode="decimal"
              :placeholder="$t('views.AdminOrgSettingsView.hitl_review_window_placeholder')"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-primary/50"
              aria-describedby="org-hitl-review-window-hint"
              data-testid="org-hitl-review-window-value"
            />
          </div>
          <div class="w-36">
            <label for="org-hitl-review-window-unit" class="mb-1.5 block text-xs font-medium text-muted-foreground">
              {{ $t('views.AdminOrgSettingsView.hitl_review_window_unit') }}
            </label>
            <AppSelect
              id="org-hitl-review-window-unit"
              v-model="hitlWindowUnit"
              :options="hitlWindowUnitOptions"
              option-label="label"
              option-value="value"
              :label="$t('views.AdminOrgSettingsView.hitl_review_window_unit')"
              class="w-full"
              data-testid="org-hitl-review-window-unit"
            />
          </div>
          <Button
            type="button"
            class="h-[42px] px-4"
            :disabled="hitlWindowSaving"
            data-testid="org-hitl-review-window-save"
            @click="saveHitlReviewWindow"
          >
            {{ hitlWindowSaving ? $t('common.saving') : $t('common.save') }}
          </Button>
        </div>

        <p id="org-hitl-review-window-hint" class="mt-2 text-xs text-muted-foreground">
          {{ $t('views.AdminOrgSettingsView.hitl_review_window_hint') }}
        </p>

        <p
          v-if="hitlWindowError"
          class="mt-2 text-sm text-destructive"
          role="alert"
          data-testid="org-hitl-review-window-error"
        >{{ hitlWindowError }}</p>
        <output
          v-else-if="hitlWindowSaved"
          class="mt-2 block text-sm text-success-text"
          data-testid="org-hitl-review-window-saved"
        >{{ $t('views.AdminOrgSettingsView.hitl_review_window_saved') }}</output>

        <div class="mt-4 rounded-lg border border-input bg-muted/30 p-3" data-testid="org-hitl-review-window-status">
          <p class="text-xs font-medium">{{ hitlWindowStatus }}</p>
        </div>
      </template>
    </SectionCard>

    <!-- Human-linked MCP authority (FAR-1972 / ADR 063 step 1): community-tier
         and org-level governance, so like the HITL review window it renders
         OUTSIDE the team-tier FeatureGate — an org admin on any plan can
         tighten how their own org's human-only gates may be decided. -->
    <SectionCard
      :title="$t('views.AdminOrgSettingsView.mcp_authority_title')"
      :description="$t('views.AdminOrgSettingsView.mcp_authority_description')"
    >
      <div v-if="mcpAuthorityLoading" class="space-y-2" aria-hidden="true" data-testid="org-mcp-authority-skeleton">
        <div class="h-4 w-48 animate-pulse rounded bg-muted" />
        <div class="h-10 w-full animate-pulse rounded bg-muted" />
      </div>

      <div v-else-if="mcpAuthorityLoadError" class="space-y-2">
        <p class="text-sm text-destructive" role="alert" data-testid="org-mcp-authority-load-error">{{ mcpAuthorityLoadError }}</p>
        <button
          type="button"
          class="text-sm font-medium text-primary underline underline-offset-2 hover:no-underline"
          data-testid="org-mcp-authority-retry"
          @click="loadMcpAuthority"
        >
          {{ $t('views.AdminOrgSettingsView.mcp_authority_retry') }}
        </button>
      </div>

      <template v-else>
        <div class="flex min-h-10 items-center justify-between gap-4">
          <div>
            <p class="text-sm font-medium">{{ $t('views.AdminOrgSettingsView.mcp_authority_toggle') }}</p>
            <p class="text-xs text-muted-foreground" data-testid="org-mcp-authority-status">{{ mcpAuthorityStatus }}</p>
          </div>
          <ToggleSwitch
            :checked="mcpAuthorityEnabled"
            :disabled="mcpAuthoritySaving"
            :toggling="mcpAuthoritySaving"
            :label="$t('views.AdminOrgSettingsView.mcp_authority_toggle')"
            data-testid="org-mcp-authority-toggle"
            @toggle="toggleMcpAuthority"
          />
        </div>
        <p class="mt-3 text-xs text-muted-foreground">{{ $t('views.AdminOrgSettingsView.mcp_authority_hint') }}</p>
        <p
          v-if="mcpAuthorityError"
          class="mt-2 text-sm text-destructive"
          role="alert"
          data-testid="org-mcp-authority-error"
        >{{ mcpAuthorityError }}</p>
        <p
          v-else-if="mcpAuthoritySaved"
          class="mt-2 text-sm text-success"
          aria-live="polite"
          data-testid="org-mcp-authority-saved"
        >{{ $t('views.AdminOrgSettingsView.mcp_authority_saved') }}</p>
      </template>
    </SectionCard>

    <FeatureGate feature-name="team_rbac" required-tier="team" show-disabled>

    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadData" />

    <template v-else>
      <div class="space-y-6">
      <!-- Org Info -->
      <DataCard
        :title="$t('views.AdminOrgSettingsView.org_info')"
        variant="context"
        heading-level="h2"
      >
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
                {{ orgInfo.planTier === 'team' ? $t('views.AdminOrgSettingsView.plan_team') : $t('views.AdminOrgSettingsView.plan_community') }}
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
      </DataCard>

      <!-- Data Export -->
      <SectionCard
        :title="$t('views.AdminOrgSettingsView.data_export')"
        :description="$t('views.AdminOrgSettingsView.data_export_description')"
      >

        <div v-if="exportStatus === 'idle'" class="flex items-center gap-3">
          <Button type="button" class="h-8 px-2.5" data-testid="org-export-start" @click="startExport">
            {{ $t('views.AdminOrgSettingsView.export_all_data') }}
          </Button>
        </div>

        <div v-else-if="exportStatus === 'loading'" class="space-y-2" aria-hidden="true" data-testid="org-export-skeleton">
          <div class="h-4 w-40 animate-pulse rounded bg-muted" />
          <div class="h-8 w-32 animate-pulse rounded bg-muted" />
        </div>

        <div v-else-if="exportStatus === 'error'" class="flex items-center gap-3">
          <span class="text-sm text-destructive" role="alert" data-testid="org-export-error">
            {{ $t('views.AdminOrgSettingsView.export_failed', { error: exportError }) }}
          </span>
          <button
            type="button"
            class="text-sm font-medium text-primary-text underline underline-offset-2 hover:no-underline"
            data-testid="org-export-retry"
            @click="startExport"
          >
            {{ $t('views.AdminOrgSettingsView.retry') }}
          </button>
        </div>

        <div v-else-if="exportStatus === 'complete'" class="flex items-center gap-3">
          <span class="badge badge-status-success">{{ $t('views.AdminOrgSettingsView.export_ready') }}</span>
          <span class="text-sm text-muted-foreground" data-testid="org-export-exported-at">
            {{ $t('views.AdminOrgSettingsView.exported_at', { date: formatDate(exportData.exportedAt) }) }}
          </span>
          <button
            type="button"
            class="inline-flex h-8 items-center justify-center gap-1.5 rounded-lg border border-input bg-background px-2.5 text-sm font-medium hover:bg-muted transition-all"
            data-testid="org-export-download"
            @click="downloadExport"
          >
            {{ $t('views.AdminOrgSettingsView.download') }}
          </button>
          <button
            type="button"
            class="text-sm font-medium text-primary-text underline underline-offset-2 hover:no-underline"
            data-testid="org-export-again"
            @click="resetExport"
          >
            {{ $t('views.AdminOrgSettingsView.export_again') }}
          </button>
        </div>
      </SectionCard>

      <!-- Product Analytics -->
      <ProductAnalyticsSettings />

      <!-- Community Objects -->
      <SectionCard
        :title="$t('views.AdminOrgSettingsView.community_objects')"
        :description="$t('views.AdminOrgSettingsView.community_objects_description')"
      >
        <div class="flex items-center justify-between">
          <div>
            <p class="text-sm font-medium">{{ $t('views.AdminOrgSettingsView.community_objects_toggle') }}</p>
            <p class="text-xs text-muted-foreground">{{ $t('views.AdminOrgSettingsView.community_objects_toggle_hint') }}</p>
          </div>
          <ToggleSwitch
            :checked="communityObjectsEnabled"
            :disabled="communityObjectsSaving"
            :toggling="communityObjectsSaving"
            :label="$t('views.AdminOrgSettingsView.community_objects_toggle')"
            data-testid="community-objects-toggle"
            @toggle="toggleCommunityObjects"
          />
        </div>
        <div v-if="communityObjectsError" class="mt-2 text-xs text-destructive">{{ communityObjectsError }}</div>
      </SectionCard>

      <!-- Delete Organization -->
      <SectionCard
        :title="$t('views.AdminOrgSettingsView.delete_org')"
        :description="$t('views.AdminOrgSettingsView.delete_org_description')"
        class="border-destructive/30"
        title-class="text-destructive"
        description-class="text-destructive-text"
      >
        <Button
          type="button"
          severity="danger"
          class="h-8 px-2.5"
          data-testid="org-delete-open"
          @click="deleteDialogOpen = true"
        >
          {{ $t('views.AdminOrgSettingsView.delete_org') }}
        </Button>
      </SectionCard>
      </div>
    </template>

    <FormDialog
      :open="deleteDialogOpen"
      @update:open="deleteDialogOpen = false"
      :title="$t('views.AdminOrgSettingsView.delete_org')"
      :description="$t('views.AdminOrgSettingsView.delete_org_description')"
      :confirmText="$t('views.AdminOrgSettingsView.permanently_delete')"
      :confirmDisabled="confirmName !== orgInfo.name || deleting"
      :loading="deleting"
      @confirm="confirmDelete"
    >
      <p class="text-sm text-muted-foreground">
        {{ $t('views.AdminOrgSettingsView.delete_confirm_body', { name: orgInfo.name }) }}
        <br /><br />
        <span class="font-semibold text-destructive">{{ $t('views.AdminOrgSettingsView.this_action_cannot_be_undone') }}</span>
      </p>
      <div class="space-y-3">
        <label for="org-delete-confirm-input" class="text-sm text-muted-foreground">
          {{ $t('views.AdminOrgSettingsView.delete_confirm_label', { name: orgInfo.name }) }}
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
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import AppSelect from '../components/shared/AppSelect.vue'
import SectionCard from '../components/shared/SectionCard.vue'
import DataCard from '../components/ui/DataCard.vue'
import { ref, computed, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FormDialog from '../components/shared/FormDialog.vue'
import ToggleSwitch from '../components/shared/ToggleSwitch.vue'
import ProductAnalyticsSettings from '../components/product-analytics/ProductAnalyticsSettings.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import { shortId } from '../utils/format'
import { formatApiError } from '../lib/api/formatError'
import { formatDateShort, formatDateFilename } from '../lib/formatDate'

const planStore = usePlanStore()
const router = useRouter()
const { t } = useI18n()

const { data: orgData, loading, error: loadError, load: loadData } = useDataFetch(
  async () => {
    const [overviewResp, orgResp] = await Promise.all([
      api.GET('/api/v1/admin/billing/overview').catch(() => null),
      api.GET('/api/v1/admin/org').catch(() => null),
    ])
    if (!overviewResp || overviewResp.error) {
      return {
        error: {
          detail: t('views.AdminOrgSettingsView.load_failed', {
            error: formatApiError(overviewResp?.error),
          }),
        },
      }
    }
    if (!orgResp || orgResp.error) {
      return {
        error: {
          detail: t('views.AdminOrgSettingsView.load_failed', {
            error: formatApiError(orgResp?.error),
          }),
        },
      }
    }
    const overview = overviewResp.data
    const orgProfile = orgResp.data
    return {
      data: {
        id: orgProfile.id ?? '',
        name: orgProfile.name ?? t('views.AdminOrgSettingsView.unnamed_org'),
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

const communityObjectsEnabled = ref(true)
const communityObjectsSaving = ref(false)
const communityObjectsError = ref<string | null>(null)

async function loadCommunityObjects() {
  try {
    const resp = await api.GET('/api/v1/admin/org/community-objects')
    if (!resp.error && resp.data) {
      communityObjectsEnabled.value = resp.data.community_objects_enabled ?? true
    }
  } catch {
    // fail-open: default is enabled
  }
}

async function toggleCommunityObjects(next: boolean) {
  communityObjectsSaving.value = true
  communityObjectsError.value = null
  try {
    const resp = await api.PUT('/api/v1/admin/org/community-objects', {
      body: { community_objects_enabled: next },
    })
    if (resp.error) {
      communityObjectsError.value = formatApiError(resp.error)
    } else {
      communityObjectsEnabled.value = next
    }
  } catch (e: unknown) {
    communityObjectsError.value = formatApiError(e)
  } finally {
    communityObjectsSaving.value = false
  }
}

// ---------------------------------------------------------------------------
// HITL review window — ORG default (FAR-1257).
//
// The API contract is seconds (envelope 60..604800); the form shows a value +
// unit select so the operator does not have to think in raw seconds. Empty
// value = no org default (the PUT body sends null, which removes the key and
// falls through to the instance default).
// ---------------------------------------------------------------------------

type HitlWindowUnit = 'seconds' | 'minutes' | 'hours' | 'days'

const HITL_WINDOW_MIN_SECONDS = 60
const HITL_WINDOW_MAX_SECONDS = 604800

const HITL_WINDOW_UNIT_SECONDS: Record<HitlWindowUnit, number> = {
  seconds: 1,
  minutes: 60,
  hours: 3600,
  days: 86400,
}

type HitlWindowFormResult =
  | { kind: 'clear' }
  | { kind: 'invalid' }
  | { kind: 'ok'; seconds: number }

/**
 * Resolve the value+unit form into the seconds the API expects.
 * `clear` (empty field) maps to a null PUT body; anything that cannot be a
 * whole-envelope value is `invalid` so the caller can refuse the save rather
 * than send a value the backend would 422 on.
 */
function resolveHitlWindowForm(value: string | number, unit: HitlWindowUnit): HitlWindowFormResult {
  const raw = String(value ?? '').trim()
  if (raw === '') return { kind: 'clear' }
  const parsed = Number(raw)
  if (!Number.isFinite(parsed)) return { kind: 'invalid' }
  const seconds = Math.round(parsed * HITL_WINDOW_UNIT_SECONDS[unit])
  if (seconds < HITL_WINDOW_MIN_SECONDS || seconds > HITL_WINDOW_MAX_SECONDS) return { kind: 'invalid' }
  return { kind: 'ok', seconds }
}

/** Largest whole unit the value divides evenly by, so 86400 reads "1 Days". */
function pickHitlWindowUnit(seconds: number): HitlWindowUnit {
  if (seconds % HITL_WINDOW_UNIT_SECONDS.days === 0) return 'days'
  if (seconds % HITL_WINDOW_UNIT_SECONDS.hours === 0) return 'hours'
  if (seconds % HITL_WINDOW_UNIT_SECONDS.minutes === 0) return 'minutes'
  return 'seconds'
}

const hitlWindowLoading = ref(true)
const hitlWindowLoadError = ref<string | null>(null)
const hitlWindowValue = ref<string | number>('')
const hitlWindowUnit = ref<HitlWindowUnit>('minutes')
const hitlWindowUnitOptions = computed(() => [
  { value: 'seconds' as const, label: t('views.AdminOrgSettingsView.unit_seconds') },
  { value: 'minutes' as const, label: t('views.AdminOrgSettingsView.unit_minutes') },
  { value: 'hours' as const, label: t('views.AdminOrgSettingsView.unit_hours') },
  { value: 'days' as const, label: t('views.AdminOrgSettingsView.unit_days') },
])
const hitlWindowCurrentSeconds = ref<number | null>(null)
const hitlWindowIsDefault = ref(true)
const hitlWindowSaving = ref(false)
const hitlWindowError = ref<string | null>(null)
const hitlWindowSaved = ref(false)

const hitlWindowStatus = computed(() => {
  if (hitlWindowIsDefault.value || hitlWindowCurrentSeconds.value == null) {
    return t('views.AdminOrgSettingsView.hitl_review_window_status_default')
  }
  // Raw seconds, always >= 60, so the plural form is never wrong.
  return t('views.AdminOrgSettingsView.hitl_review_window_status_set', {
    seconds: hitlWindowCurrentSeconds.value,
  })
})

function applyHitlWindow(seconds: number | null) {
  hitlWindowCurrentSeconds.value = seconds
  hitlWindowIsDefault.value = seconds === null
  if (seconds === null) {
    hitlWindowValue.value = ''
    hitlWindowUnit.value = 'minutes'
    return
  }
  const unit = pickHitlWindowUnit(seconds)
  hitlWindowUnit.value = unit
  hitlWindowValue.value = seconds / HITL_WINDOW_UNIT_SECONDS[unit]
}

async function loadHitlReviewWindow() {
  hitlWindowLoading.value = true
  hitlWindowLoadError.value = null
  try {
    const resp = await api.GET('/api/v1/admin/org/hitl-review-window')
    if (resp.error) {
      hitlWindowLoadError.value = t('views.AdminOrgSettingsView.hitl_review_window_load_failed', {
        error: formatApiError(resp.error),
      })
      return
    }
    const data = resp.data as { hitl_review_window_seconds?: number | null; is_default?: boolean } | undefined
    // `is_default` marks the ABSENT-key case: no org default is configured.
    const seconds = data && data.is_default !== true ? (data.hitl_review_window_seconds ?? null) : null
    applyHitlWindow(seconds)
  } catch (e: unknown) {
    hitlWindowLoadError.value = t('views.AdminOrgSettingsView.hitl_review_window_load_failed', {
      error: formatApiError(e),
    })
  } finally {
    hitlWindowLoading.value = false
  }
}

async function saveHitlReviewWindow() {
  if (hitlWindowSaving.value) return
  hitlWindowError.value = null
  hitlWindowSaved.value = false
  const result = resolveHitlWindowForm(hitlWindowValue.value, hitlWindowUnit.value)
  if (result.kind === 'invalid') {
    hitlWindowError.value = t('views.AdminOrgSettingsView.hitl_review_window_out_of_range', {
      min: HITL_WINDOW_MIN_SECONDS,
      max: HITL_WINDOW_MAX_SECONDS,
    })
    return
  }
  const seconds = result.kind === 'clear' ? null : result.seconds
  hitlWindowSaving.value = true
  try {
    const resp = await api.PUT('/api/v1/admin/org/hitl-review-window', {
      body: { hitl_review_window_seconds: seconds },
    })
    if (resp.error) {
      hitlWindowError.value = t('views.AdminOrgSettingsView.hitl_review_window_save_failed', {
        error: formatApiError(resp.error),
      })
      return
    }
    applyHitlWindow(seconds)
    hitlWindowSaved.value = true
  } catch (e: unknown) {
    hitlWindowError.value = t('views.AdminOrgSettingsView.hitl_review_window_save_failed', {
      error: formatApiError(e),
    })
  } finally {
    hitlWindowSaving.value = false
  }
}

const mcpAuthorityEnabled = ref(true)
const mcpAuthorityLoading = ref(true)
const mcpAuthoritySaving = ref(false)
const mcpAuthorityLoadError = ref<string | null>(null)
const mcpAuthorityError = ref<string | null>(null)
const mcpAuthoritySaved = ref(false)

const mcpAuthorityStatus = computed(() =>
  mcpAuthorityEnabled.value
    ? t('views.AdminOrgSettingsView.mcp_authority_status_enabled')
    : t('views.AdminOrgSettingsView.mcp_authority_status_disabled'),
)

async function loadMcpAuthority() {
  mcpAuthorityLoading.value = true
  mcpAuthorityLoadError.value = null
  try {
    const resp = await api.GET('/api/v1/admin/org/mcp-human-linked-hitl-authority')
    if (resp.error) {
      mcpAuthorityLoadError.value = t('views.AdminOrgSettingsView.mcp_authority_load_failed', {
        error: formatApiError(resp.error),
      })
      return
    }
    const data = resp.data as { enabled?: boolean } | undefined
    mcpAuthorityEnabled.value = data?.enabled ?? true
  } catch (e: unknown) {
    mcpAuthorityLoadError.value = t('views.AdminOrgSettingsView.mcp_authority_load_failed', {
      error: formatApiError(e),
    })
  } finally {
    mcpAuthorityLoading.value = false
  }
}

async function toggleMcpAuthority(next: boolean) {
  mcpAuthoritySaving.value = true
  mcpAuthorityError.value = null
  mcpAuthoritySaved.value = false
  try {
    const resp = await api.PUT('/api/v1/admin/org/mcp-human-linked-hitl-authority', {
      body: { enabled: next },
    })
    if (resp.error) {
      mcpAuthorityError.value = t('views.AdminOrgSettingsView.mcp_authority_save_failed', {
        error: formatApiError(resp.error),
      })
    } else {
      mcpAuthorityEnabled.value = next
      mcpAuthoritySaved.value = true
    }
  } catch (e: unknown) {
    mcpAuthorityError.value = t('views.AdminOrgSettingsView.mcp_authority_save_failed', {
      error: formatApiError(e),
    })
  } finally {
    mcpAuthoritySaving.value = false
  }
}

function formatDate(dateStr: string): string {
  if (!dateStr) return t('views.AdminOrgSettingsView.unknown_date')
  const d = new Date(dateStr)
  return formatDateShort(d)
}

async function startExport() {
  exportStatus.value = 'loading'
  exportError.value = null
  try {
    const resp = await api.GET('/api/v1/admin/org/export')
    if (resp.error) {
      exportStatus.value = 'error'
      exportError.value = formatApiError(resp.error)
      return
    }
    const data = resp.data
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
    const resp = await api.DELETE('/api/v1/admin/org')
    if (resp.error) {
      deleteError.value = t('views.AdminOrgSettingsView.delete_failed', {
        error: formatApiError(resp.error),
      })
      deleting.value = false
      return
    }
    deleteDialogOpen.value = false
    router.push('/login')
  } catch (e: unknown) {
    deleteError.value = t('views.AdminOrgSettingsView.delete_failed', {
      error: formatApiError(e),
    })
    deleting.value = false
  }
}

planStore.fetchPlan()
loadCommunityObjects()
loadHitlReviewWindow()
loadMcpAuthority()
</script>
