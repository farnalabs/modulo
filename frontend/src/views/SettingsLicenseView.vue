<template>
  <div data-theme="agent" data-loading="false" class="page-wide">
    <PageHeader title="License" data-test-id="license-title" :subtitle="$t('views.SettingsLicenseView.manage_your_modulo_team_license_key_and_view_plan_details')" />

    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadAll" />

    <template v-else>
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

      <!-- License Key Management -->
      <div class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.SettingsLicenseView.license_key') }}</h2>

        <div v-if="licenseInfo.has_license" class="mb-6 rounded-lg bg-muted/50 p-4">
          <p class="text-xs font-medium text-muted-foreground uppercase tracking-wide">{{ $t('views.SettingsLicenseView.current_key') }}</p>
          <p class="mt-1 font-mono text-sm">{{ maskedKey }}</p>
        </div>

        <div class="space-y-3">
          <label for="settingslicenseview-field-1" class="block text-sm font-medium text-muted-foreground">{{ $t('views.SettingsLicenseView.new_license_key') }}</label>
          <textarea id="settingslicenseview-field-1"
            v-model="newLicenseKey"
            rows="4"
            data-testid="license-key-input"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono placeholder:text-muted-foreground/50 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 outline-none transition-all"
            :placeholder="$t('views.SettingsLicenseView.paste_your_modulolicensekey_value_here')"
          />
          <p v-if="verifyResult" class="text-sm" :class="verifyResult.valid ? 'text-success' : 'text-destructive'">
            {{ verifyResult.message }}
          </p>

          <div class="flex flex-wrap items-center gap-3">
            <Button data-testid="license-verify-btn" severity="secondary" outlined :disabled="!newLicenseKey.trim() || verifying" @click="verifyKey">
              {{ verifying ? $t('views.SettingsLicenseView.verifying') : $t('views.SettingsLicenseView.verify_key') }}
            </Button>
            <Button data-testid="license-apply-btn" :disabled="!newLicenseKey.trim() || applying" @click="openApplyDialog">
              {{ applying ? $t('views.SettingsLicenseView.applying') : $t('views.SettingsLicenseView.apply_key') }}
            </Button>
            <Button v-if="licenseInfo.has_license" severity="danger" :disabled="removing" @click="openRemoveDialog">
              {{ removing ? $t('views.SettingsLicenseView.removing') : $t('views.SettingsLicenseView.remove_license') }}
            </Button>
          </div>
          <p class="text-xs text-muted-foreground">
            {{ $t('views.SettingsLicenseView.restart_note') }}
          </p>
        </div>
      </div>
    </template>

    <FormDialog
      v-model:open="applyDialogOpen"
      :title="$t('views.SettingsLicenseView.apply_license_title')"
      :description="$t('views.SettingsLicenseView.apply_license_description')"
      :confirmText="$t('views.SettingsLicenseView.confirm_apply')"
      :loading="applying"
      @confirm="applyKey"
    />

    <FormDialog
      v-model:open="removeDialogOpen"
      :title="$t('views.SettingsLicenseView.remove_license_title')"
      :description="$t('views.SettingsLicenseView.remove_license_description')"
      :confirmText="$t('views.SettingsLicenseView.confirm_remove')"
      :loading="removing"
      @confirm="removeLicense"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDataFetch } from '../composables/useDataFetch'
import Button from 'primevue/button'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Badge from 'primevue/badge'
import FormDialog from '../components/shared/FormDialog.vue'
import { ExternalLink } from '@lucide/vue'
import { format } from 'date-fns'
import { shortId } from '../utils/format'

const { t } = useI18n()
const planStore = usePlanStore()

interface LicenseStatus {
  has_license: boolean
  tier: string
  features: string[]
  expires_at: string | null
  org_id: string | null
}

const defaultLicense: LicenseStatus = {
  has_license: false,
  tier: 'community',
  features: [],
  expires_at: null,
  org_id: null,
}

const { loading, error: loadError, data: licenseInfo, load: loadAll } = useDataFetch<LicenseStatus>(
  () => (api as any).GET('/api/v1/admin/license'),
  { initialValue: defaultLicense }
)

const newLicenseKey = ref('')

const verifying = ref(false)
const verifyResult = ref<{ valid: boolean; message: string } | null>(null)

const applying = ref(false)
const applyDialogOpen = ref(false)

const removing = ref(false)
const removeDialogOpen = ref(false)

const maskedKey = computed(() => {
  return licenseInfo.value.has_license ? t('views.SettingsLicenseView.team_license_key_active') : '—'
})

function formatDate(iso: string): string {
  try {
    return format(new Date(iso), 'MMMM d, yyyy')
  } catch {
    return iso
  }
}

async function verifyKey() {
  if (!newLicenseKey.value.trim()) return
  verifying.value = true
  verifyResult.value = null
  try {
    const { data, error: err } = await (api as any).POST('/api/v1/admin/license', {
      body: { license_key: newLicenseKey.value.trim() },
    })
    if (err) {
      verifyResult.value = { valid: false, message: String(err) }
    } else {
      verifyResult.value = {
        valid: true,
        message: t('views.SettingsLicenseView.valid_license_key', {
          tier: data.tier,
          expires: data.expires_at ? formatDate(data.expires_at) : t('views.SettingsLicenseView.never'),
        }),
      }
    }
  } catch (e: unknown) {
    verifyResult.value = { valid: false, message: formatApiError(e) }
  } finally {
    verifying.value = false
  }
}

function openApplyDialog() {
  verifyResult.value = null
  applyDialogOpen.value = true
}

async function applyKey() {
  if (!newLicenseKey.value.trim()) return
  applying.value = true
  try {
    const { error: err } = await (api as any).POST('/api/v1/admin/license', {
      body: { license_key: newLicenseKey.value.trim() },
    })
    if (err) {
      verifyResult.value = { valid: false, message: `${t('views.SettingsLicenseView.failed_to_apply')} ${formatApiError(err)}` }
    } else {
      applyDialogOpen.value = false
      verifyResult.value = null
      newLicenseKey.value = ''
      await planStore.fetchPlan()
      await loadAll()
    }
  } catch (e: unknown) {
    verifyResult.value = { valid: false, message: `${t('views.SettingsLicenseView.failed_to_apply')} ${formatApiError(e)}` }
  } finally {
    applying.value = false
  }
}

function openRemoveDialog() {
  removeDialogOpen.value = true
}

async function removeLicense() {
  removing.value = true
  try {
    const { error: err } = await (api as any).DELETE('/api/v1/admin/license')
    if (err) {
      verifyResult.value = { valid: false, message: `${t('views.SettingsLicenseView.failed_to_remove')} ${formatApiError(err)}` }
    } else {
      removeDialogOpen.value = false
      await planStore.fetchPlan()
      await loadAll()
    }
  } catch (e: unknown) {
    verifyResult.value = { valid: false, message: `${t('views.SettingsLicenseView.failed_to_remove')} ${formatApiError(e)}` }
  } finally {
    removing.value = false
  }
}

onMounted(() => {
  planStore.fetchPlan()
})
</script>
