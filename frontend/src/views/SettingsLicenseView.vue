<template>
  <div data-theme="agent" data-loading="false" class="page-wide">
    <PageHeader title="License" data-testid="license-title" :subtitle="$t('views.SettingsLicenseView.manage_your_modulo_team_license_key_and_view_plan_details')" />

    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadAll" />

    <template v-else>
      <!--
        The renewal countdown / grace dialog is NOT rendered here. It lives in
        the authenticated shell (components/AppLayout.vue) so it is visible on
        every route; rendering it here as well would show two banners and two
        grace dialogs on this one page. This page only reads the same status.
      -->

      <!-- Current Tier Card -->
      <div class="rounded-lg border bg-card p-6 shadow-sm">
        <div v-if="licenseInfo.tier === 'team'" class="flex items-start justify-between">
          <div>
            <div class="flex items-center gap-2">
              <h2 class="text-base font-semibold">{{ $t('views.SettingsLicenseView.team') }}</h2>
              <Badge severity="info">{{ $t('common.active') }}</Badge>
            </div>
            <p v-if="licenseInfo.org_id" class="mt-2 text-sm text-muted-foreground">
              {{ $t('views.SettingsLicenseView.licensed_to') }} <span class="font-medium text-foreground" :title="licenseInfo.org_id"><span class="select-all font-mono">{{ shortId(licenseInfo.org_id) }}</span></span>
            </p>
            <p v-if="licenseInfo.expires_at" class="mt-1 text-sm text-muted-foreground">
              {{ $t('views.SettingsLicenseView.expires') }} <span class="font-medium text-foreground">{{ formatDate(licenseInfo.expires_at) }}</span>
            </p>
          </div>
        </div>
        <div v-else>
          <div class="flex items-center gap-2">
            <h2 class="text-base font-semibold">{{ $t('views.SettingsLicenseView.community') }}</h2>
            <Badge severity="secondary" class="border border-border">{{ $t('views.SettingsLicenseView.community') }}</Badge>
          </div>
          <p class="mt-2 text-sm text-muted-foreground">
            {{ $t('views.SettingsLicenseView.tier_upgrade_sentence', { currentTier: planStore.getTierLabel(licenseInfo.tier), teamTier: planStore.getTierLabel('team') }) }}
          </p>
          <Button as="a" href="https://modulo.run/pricing" target="_blank" rel="noopener noreferrer" class="mt-4 border-primary/30 hover:border-primary/60">
            {{ $t('views.SettingsLicenseView.get_team_license') }}
            <ExternalLink class="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      <!--
        FAR-2104: two scopes, two audiences. The licence is an org-level object
        (`org.license.manage`), so an org admin manages the organisation's own
        key here; the instance-wide key stays a system-admin control
        (`system.config.manage`). Both gates are the permissions the endpoints
        enforce — see composables/useLicenceAccess.ts.
      -->

      <!-- Organisation licence (org admin) -->
      <div v-if="canManageOrgLicence && orgId" data-testid="licence-org-controls" class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="text-base font-semibold">{{ $t('views.SettingsLicenseView.org_license_title') }}</h2>
        <p class="mt-1 text-sm text-muted-foreground">
          {{ $t('views.SettingsLicenseView.org_license_scope') }}
        </p>

        <div v-if="licenseInfo.has_license" class="mt-4 rounded-lg bg-muted/50 p-4">
          <p class="text-xs font-medium text-muted-foreground uppercase tracking-wide">{{ $t('views.SettingsLicenseView.current_key') }}</p>
          <p class="mt-1 font-mono text-sm">{{ maskedKey }}</p>
        </div>

        <div class="mt-4 space-y-3">
          <label for="settingslicenseview-org-key" class="block text-sm font-medium text-muted-foreground">{{ $t('views.SettingsLicenseView.new_license_key') }}</label>
          <textarea id="settingslicenseview-org-key"
            v-model="newLicenseKey"
            rows="4"
            data-testid="license-key-input"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono placeholder:text-muted-foreground/50 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 outline-none transition-all"
            :placeholder="$t('views.SettingsLicenseView.paste_your_modulolicensekey_value_here')"
          />

          <div class="flex flex-wrap items-center gap-3">
            <Button data-testid="license-apply-btn" :disabled="!newLicenseKey.trim() || applying" @click="openApplyDialog">
              {{ applying ? $t('views.SettingsLicenseView.applying') : $t('views.SettingsLicenseView.apply_key') }}
            </Button>
            <Button v-if="licenseInfo.has_license" data-testid="license-remove-btn" severity="danger" :disabled="removing" @click="openRemoveDialog('org')">
              {{ removing ? $t('views.SettingsLicenseView.removing') : $t('views.SettingsLicenseView.remove_license') }}
            </Button>
          </div>
          <p class="text-xs text-muted-foreground">
            {{ $t('views.SettingsLicenseView.org_license_note') }}
          </p>
        </div>
      </div>

      <!-- System licence (system admin only) -->
      <div v-if="canManageSystemLicence" data-testid="licence-system-controls" class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="text-base font-semibold">{{ $t('views.SettingsLicenseView.system_license_title') }}</h2>
        <p class="mt-1 text-sm text-muted-foreground">
          {{ $t('views.SettingsLicenseView.system_license_scope') }}
        </p>

        <div class="mt-4 space-y-3">
          <label for="settingslicenseview-system-key" class="block text-sm font-medium text-muted-foreground">{{ $t('views.SettingsLicenseView.new_license_key') }}</label>
          <textarea id="settingslicenseview-system-key"
            v-model="newSystemLicenseKey"
            rows="4"
            data-testid="license-key-input-system"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono placeholder:text-muted-foreground/50 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 outline-none transition-all"
            :placeholder="$t('views.SettingsLicenseView.paste_your_modulolicensekey_value_here')"
          />

          <div class="flex flex-wrap items-center gap-3">
            <!--
              FAR-2154: there is ONE licence action per scope. The removed
              "Verify Key" button called the same POST /api/v1/admin/license
              that installs the key, so "verify" silently installed it and
              busted the status cache — a verification that mutated live state.
              Installing is the only meaningful operation, so the confirm
              dialog is the review step (it shows the key's own claims before
              committing) and no separate verify control exists.
            -->
            <Button data-testid="license-apply-btn-system" :disabled="!newSystemLicenseKey.trim() || applying" @click="openSystemApplyDialog">
              {{ applying ? $t('views.SettingsLicenseView.applying') : $t('views.SettingsLicenseView.install_system_license') }}
            </Button>
            <!-- FAR-2110: remove the INSTANCE-WIDE licence (system scope). -->
            <Button v-if="licenseInfo.has_license" data-testid="license-remove-btn-system" severity="danger" :disabled="removing" @click="openRemoveDialog('system')">
              {{ removing ? $t('views.SettingsLicenseView.removing') : $t('views.SettingsLicenseView.remove_license') }}
            </Button>
          </div>
          <p class="text-xs text-muted-foreground">
            {{ $t('views.SettingsLicenseView.system_license_note') }}
          </p>
        </div>
      </div>

      <p v-if="licenceError" class="text-sm text-destructive" aria-live="polite">
        {{ licenceError }}
      </p>
    </template>

    <FormDialog
      v-model:open="applyDialogOpen"
      :title="applyScope === 'system' ? $t('views.SettingsLicenseView.install_license_title') : $t('views.SettingsLicenseView.apply_license_title')"
      :description="applyDialogDescription"
      :confirmText="applyScope === 'system' ? $t('views.SettingsLicenseView.install_system_license') : $t('views.SettingsLicenseView.confirm_apply')"
      :loading="applying"
      @confirm="confirmApply"
    >
      <!--
        FAR-2154: the review step. Installing is the ONLY licence write, so the
        operator sees what the key claims (decoded locally, unverified) before
        the request that both validates and installs it.
      -->
      <div v-if="licenceKeyPreview" data-testid="licence-key-preview" class="space-y-2">
        <dl class="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
          <div v-if="licenceKeyPreview.tier">
            <dt class="text-xs text-muted-foreground">{{ $t('views.SettingsLicenseView.preview_tier') }}</dt>
            <dd class="font-medium">{{ licenceKeyPreview.tier }}</dd>
          </div>
          <div v-if="licenceKeyPreview.expiresAt">
            <dt class="text-xs text-muted-foreground">{{ $t('views.SettingsLicenseView.preview_expires') }}</dt>
            <dd class="font-medium">{{ formatDate(licenceKeyPreview.expiresAt) }}</dd>
          </div>
          <div v-if="licenceKeyPreview.startsAt">
            <dt class="text-xs text-muted-foreground">{{ $t('views.SettingsLicenseView.preview_starts') }}</dt>
            <dd class="font-medium">{{ formatDate(licenceKeyPreview.startsAt) }}</dd>
          </div>
        </dl>
        <p class="text-xs text-muted-foreground">
          {{ $t('views.SettingsLicenseView.preview_unverified') }}
        </p>
      </div>
      <p v-else class="text-sm text-muted-foreground">
        {{ $t('views.SettingsLicenseView.preview_unreadable') }}
      </p>
    </FormDialog>

    <FormDialog
      v-model:open="removeDialogOpen"
      :title="$t('views.SettingsLicenseView.remove_license_title')"
      :description="$t('views.SettingsLicenseView.remove_license_description')"
      :confirmText="$t('views.SettingsLicenseView.confirm_remove')"
      :loading="removing"
      @confirm="confirmRemove"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, inject, onMounted, type ComputedRef, type WritableComputedRef } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDataFetch } from '../composables/useDataFetch'
import { useLicenceAccess } from '../composables/useLicenceAccess'
import { useCurrentUser } from '../composables/useCurrentUser'
import Button from 'primevue/button'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { decodeBase64Url } from '../lib/jwt'
import { usePlanStore } from '../stores/planStore'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Badge from 'primevue/badge'
import FormDialog from '../components/shared/FormDialog.vue'
import { ExternalLink } from '@lucide/vue'
import { format } from 'date-fns'
import { shortId } from '../utils/format'
import type { components } from '../lib/api/schema'

const { t } = useI18n()
const planStore = usePlanStore()

// FAR-2104: the two licence scopes, each gated by the permission its
// endpoints enforce (never a parallel role check).
const { canManageOrgLicence, canManageSystemLicence } = useLicenceAccess()
const { orgId } = useCurrentUser()

const licenceError = ref<string | null>(null)

// The wire shape is the generated OpenAPI type, never a hand-maintained copy:
// a renamed backend field fails `vue-tsc` here instead of rendering `undefined`.
type LicenseStatus = components['schemas']['LicenseStatusResponse']

const defaultLicense: LicenseStatus = {
  has_license: false,
  tier: 'community',
  features: [],
  expires_at: null,
  org_id: null,
  renewal_mode: 'manual',
  licence_state: 'none',
}

/**
 * FAR-2075: the authenticated shell (components/AppLayout.vue) owns the
 * licence-status read because it also renders the renewal notice on every
 * authenticated route. It provides that query under this key — the string is
 * duplicated there because no module can be imported by both without pulling
 * the whole shell into this page's chunk.
 *
 * Mounted under the shell (the real application) this page REUSES the shell's
 * query: navigating here costs no second GET, and applying/removing a key
 * refreshes the shell's banner too. Mounted standalone (unit tests, or any
 * future embed without the shell) it falls back to its own read.
 */
const LICENCE_STATUS_INJECTION_KEY = 'modulo:licence-status'

interface LicenceStatusSource {
  data: WritableComputedRef<LicenseStatus>
  loading: ComputedRef<boolean>
  error: WritableComputedRef<string | null>
  load: () => Promise<void>
}

const providedLicence = inject<LicenceStatusSource | null>(LICENCE_STATUS_INJECTION_KEY, null)

const ownLicence = useDataFetch<LicenseStatus>(
  () => api.GET('/api/v1/admin/license'),
  { initialValue: defaultLicense, immediate: providedLicence === null }
)

const licenseInfo = computed(() =>
  providedLicence ? providedLicence.data.value : ownLicence.data.value
)
const loading = computed(() =>
  providedLicence ? providedLicence.loading.value : ownLicence.loading.value
)
const loadError = computed(() =>
  providedLicence ? providedLicence.error.value : ownLicence.error.value
)

async function loadAll(): Promise<void> {
  if (providedLicence) {
    await providedLicence.load()
    return
  }
  await ownLicence.load()
}

const newLicenseKey = ref('')
const newSystemLicenseKey = ref('')

const applying = ref(false)
const applyDialogOpen = ref(false)
// Which scope the open confirmation dialog installs into.
const applyScope = ref<'org' | 'system'>('org')

/**
 * FAR-2142: the two scopes differ in DURABILITY, so one confirmation text
 * cannot describe both honestly.
 *
 * An organisation key is written to `org.settings_json["license_key"]` (a
 * database row), so it survives a restart. The instance-wide key is held only
 * in the API process's memory (`core.license.store_license`), so a restart or
 * redeploy discards it unless `MODULO_LICENSE_KEY` is also configured — the
 * operator needs to know that BEFORE they rely on it.
 */
const applyDialogDescription = computed(() =>
  applyScope.value === 'system'
    ? t('views.SettingsLicenseView.apply_license_description_system')
    : t('views.SettingsLicenseView.apply_license_description_org')
)

const removing = ref(false)
const removeDialogOpen = ref(false)
// Which scope the open remove confirmation clears (FAR-2110).
const removeScope = ref<'org' | 'system'>('org')

const maskedKey = computed(() => {
  return licenseInfo.value.has_license ? t('views.SettingsLicenseView.team_license_key_active') : '—'
})

function formatDate(iso: string): string {
  const parsed = new Date(iso)
  return Number.isNaN(parsed.getTime()) ? iso : format(parsed, 'MMMM d, yyyy')
}

/**
 * Decode a licence key's payload segment for display only.
 *
 * The wire format is `<base64url(payload)>.<base64url(signature)>` (see
 * `backend/src/modulo/core/license.py`). This reads the claims so the operator
 * can review them in the confirmation dialog; it does NOT verify the
 * signature and nothing is trusted from it — the install endpoint is what
 * validates the key. Returns null on any decode/parse failure so an
 * unreadable key still reaches the server's actionable error.
 */
function decodeLicenceKeyPayload(raw: string): Record<string, unknown> | null {
  const segment = raw.trim().split('.')[0]
  if (!segment) return null
  try {
    const binary = decodeBase64Url(segment)
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0))
    return JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>
  } catch {
    return null
  }
}

interface LicenceKeyPreview {
  tier: string | null
  expiresAt: string | null
  startsAt: string | null
}

const licenceKeyPreview = computed<LicenceKeyPreview | null>(() => {
  const raw = applyScope.value === 'system' ? newSystemLicenseKey.value : newLicenseKey.value
  const payload = decodeLicenceKeyPayload(raw)
  if (!payload) return null
  const preview: LicenceKeyPreview = { tier: null, expiresAt: null, startsAt: null }
  if (typeof payload.tier === 'string') preview.tier = payload.tier
  if (typeof payload.expires_at === 'string') preview.expiresAt = payload.expires_at
  if (typeof payload.starts_at === 'string') preview.startsAt = payload.starts_at
  return preview
})

function openApplyDialog() {
  licenceError.value = null
  applyScope.value = 'org'
  applyDialogOpen.value = true
}

function confirmApply() {
  return applyScope.value === 'system' ? applySystemKey() : applyKey()
}

/** Install the ORGANISATION's key — the org admin's own surface. */
async function applyKey() {
  if (!newLicenseKey.value.trim() || !orgId.value) return
  applying.value = true
  try {
    const { error: err } = await api.PUT('/api/v1/admin/orgs/{org_id}/license', {
      params: { path: { org_id: orgId.value } },
      body: { license_key: newLicenseKey.value.trim() },
    })
    if (err) {
      licenceError.value = `${t('views.SettingsLicenseView.failed_to_apply')} ${formatApiError(err)}`
    } else {
      applyDialogOpen.value = false
      licenceError.value = null
      newLicenseKey.value = ''
      await planStore.fetchPlan()
      await loadAll()
    }
  } catch (e: unknown) {
    licenceError.value = `${t('views.SettingsLicenseView.failed_to_apply')} ${formatApiError(e)}`
  } finally {
    applying.value = false
  }
}

function openSystemApplyDialog() {
  licenceError.value = null
  applyScope.value = 'system'
  applyDialogOpen.value = true
}

/** Install the INSTANCE-WIDE key — system admin only. */
async function applySystemKey() {
  if (!newSystemLicenseKey.value.trim()) return
  applying.value = true
  try {
    const { error: err } = await api.POST('/api/v1/admin/license', {
      body: { license_key: newSystemLicenseKey.value.trim() },
    })
    if (err) {
      licenceError.value = `${t('views.SettingsLicenseView.failed_to_apply')} ${formatApiError(err)}`
      return
    }
    applyDialogOpen.value = false
    licenceError.value = null
    newSystemLicenseKey.value = ''
    await planStore.fetchPlan()
    await loadAll()
  } catch (e: unknown) {
    licenceError.value = `${t('views.SettingsLicenseView.failed_to_apply')} ${formatApiError(e)}`
  } finally {
    applying.value = false
  }
}

function openRemoveDialog(scope: 'org' | 'system') {
  removeScope.value = scope
  removeDialogOpen.value = true
}

function confirmRemove() {
  return removeScope.value === 'system' ? removeSystemLicense() : removeLicense()
}

/** Clear the ORGANISATION's own key (falls back to the system licence). */
async function removeLicense() {
  if (!orgId.value) return
  removing.value = true
  try {
    const { error: err } = await api.DELETE('/api/v1/admin/orgs/{org_id}/license', {
      params: { path: { org_id: orgId.value } },
    })
    if (err) {
      licenceError.value = `${t('views.SettingsLicenseView.failed_to_remove')} ${formatApiError(err)}`
    } else {
      removeDialogOpen.value = false
      await planStore.fetchPlan()
      await loadAll()
    }
  } catch (e: unknown) {
    licenceError.value = `${t('views.SettingsLicenseView.failed_to_remove')} ${formatApiError(e)}`
  } finally {
    removing.value = false
  }
}

/**
 * Clear the INSTANCE-WIDE key — system admin only (FAR-2110).
 *
 * `DELETE /api/v1/admin/license` clears the in-memory system licence and
 * busts every cached licence status server-side, so the deployment degrades
 * without a restart. Reloading the plan and the status is what the operator
 * sees change immediately.
 */
async function removeSystemLicense() {
  removing.value = true
  try {
    const { error: err } = await api.DELETE('/api/v1/admin/license')
    if (err) {
      licenceError.value = `${t('views.SettingsLicenseView.failed_to_remove')} ${formatApiError(err)}`
    } else {
      removeDialogOpen.value = false
      await planStore.fetchPlan()
      await loadAll()
    }
  } catch (e: unknown) {
    licenceError.value = `${t('views.SettingsLicenseView.failed_to_remove')} ${formatApiError(e)}`
  } finally {
    removing.value = false
  }
}

onMounted(() => {
  planStore.fetchPlan()
})
</script>
