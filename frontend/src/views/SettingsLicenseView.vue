<template>
  <div data-theme="agent" data-loading="false" class="page-narrow">
    <header>
      <h1 data-testid="license-title" class="text-2xl font-semibold tracking-tight">License</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.SettingsLicenseView.manage_your_modulo_team_license_key_and_view_plan_details') }}</p>
    </header>

    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadAll" />

    <template v-else>
      <!-- Current Tier Card -->
      <div class="rounded-lg border bg-card p-6 shadow-sm">
        <div v-if="licenseInfo.tier === 'team'" class="flex items-start justify-between">
          <div>
            <div class="flex items-center gap-2">
              <h2 class="text-base font-semibold">Team</h2>
              <Badge variant="default">Active</Badge>
            </div>
            <p v-if="licenseInfo.org_id" class="mt-2 text-sm text-muted-foreground">
              Licensed to <span class="font-medium text-foreground">{{ licenseInfo.org_id }}</span>
            </p>
            <p v-if="licenseInfo.expires_at" class="mt-1 text-sm text-muted-foreground">
              Expires <span class="font-medium text-foreground">{{ formatDate(licenseInfo.expires_at) }}</span>
            </p>
          </div>
        </div>
        <div v-else>
          <div class="flex items-center gap-2">
            <h2 class="text-base font-semibold">Community</h2>
            <Badge variant="outline">Community</Badge>
          </div>
          <p class="mt-2 text-sm text-muted-foreground">
            You are currently on the {{ planStore.getTierLabel(licenseInfo.tier) }} tier. Upgrade to {{ planStore.getTierLabel('team') }} to unlock all features.
          </p>
          <Button
            variant="default"
            as="a"
            href="https://modulo.run/pricing"
            target="_blank"
            rel="noopener noreferrer"
            class="btn-glow mt-4 border-primary/30 hover:border-primary/60"
          >
            Get a Team License
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          </Button>
        </div>
      </div>

      <!-- License Key Management -->
      <div class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.AdminFeatureFlagsView.license_key') }}</h2>

        <div v-if="licenseInfo.has_license" class="mb-6 rounded-lg bg-muted/50 p-4">
          <p class="text-xs font-medium text-muted-foreground uppercase tracking-wide">{{ $t('views.SettingsLicenseView.current_key') }}</p>
          <p class="mt-1 font-mono text-sm">{{ maskedKey }}</p>
        </div>

        <div class="space-y-3">
          <label class="block text-sm font-medium text-muted-foreground">{{ $t('views.SettingsLicenseView.new_license_key') }}</label>
          <textarea
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
            <Button data-testid="license-verify-btn" variant="outline" :disabled="!newLicenseKey.trim() || verifying" @click="verifyKey">
              {{ verifying ? 'Verifying...' : 'Verify Key' }}
            </Button>
            <Button data-testid="license-apply-btn" :disabled="!newLicenseKey.trim() || applying" @click="openApplyDialog">
              {{ applying ? 'Applying...' : 'Apply Key' }}
            </Button>
            <Button v-if="licenseInfo.has_license" variant="destructive" :disabled="removing" @click="openRemoveDialog">
              {{ removing ? 'Removing...' : 'Remove License' }}
            </Button>
          </div>
          <p class="text-xs text-muted-foreground">
            Applying a new license key requires a server restart to take full effect.
          </p>
        </div>
      </div>
    </template>

    <!-- Apply Confirmation Dialog -->
    <Dialog v-model:open="applyDialogOpen">
      <DialogContent class="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Apply License Key</DialogTitle>
          <DialogDescription>
            This will replace your current license key. Applying a new license key requires a server restart to take full effect.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter class="gap-2 sm:justify-end">
          <Button variant="outline" @click="applyDialogOpen = false">Cancel</Button>
          <Button :disabled="applying" @click="applyKey">
            {{ applying ? 'Applying...' : 'Confirm Apply' }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- Remove Confirmation Dialog -->
    <Dialog v-model:open="removeDialogOpen">
      <DialogContent class="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Remove License</DialogTitle>
          <DialogDescription>
            Are you sure you want to remove the Team license? Your instance will revert to Community tier and all Team features will be disabled.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter class="gap-2 sm:justify-end">
          <Button variant="outline" @click="removeDialogOpen = false">Cancel</Button>
          <Button variant="destructive" :disabled="removing" @click="removeLicense">
            {{ removing ? 'Removing...' : 'Confirm Remove' }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Button } from '@/components/ui/button'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Badge } from '../components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../components/ui/dialog'

const planStore = usePlanStore()

const loading = ref(true)
const loadError = ref<string | null>(null)

interface LicenseStatus {
  has_license: boolean
  tier: string
  features: string[]
  expires_at: string | null
  org_id: string | null
}

const licenseInfo = ref<LicenseStatus>({
  has_license: false,
  tier: 'community',
  features: [],
  expires_at: null,
  org_id: null,
})

const newLicenseKey = ref('')

const verifying = ref(false)
const verifyResult = ref<{ valid: boolean; message: string } | null>(null)

const applying = ref(false)
const applyDialogOpen = ref(false)

const removing = ref(false)
const removeDialogOpen = ref(false)

const maskedKey = computed(() => {
  return 'Team license key active'
})

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    })
  } catch {
    return iso
  }
}

async function loadAll() {
  loading.value = true
  loadError.value = null
  try {
    const licResp = await (api as any).GET('/api/v1/admin/license')

    if (licResp.error) {
      loadError.value = `Failed to load license: ${formatApiError(licResp.error)}`
      return
    }
    licenseInfo.value = licResp.data as LicenseStatus
  } catch (e: unknown) {
    loadError.value = `Failed to load data: ${formatApiError(e)}`
  } finally {
    loading.value = false
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
        message: `Valid license key — Tier: ${data.tier}, expires: ${data.expires_at ? formatDate(data.expires_at) : 'never'}`,
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
      verifyResult.value = { valid: false, message: `Failed to apply: ${formatApiError(err)}` }
    } else {
      applyDialogOpen.value = false
      verifyResult.value = null
      newLicenseKey.value = ''
      await planStore.fetchPlan()
      await loadAll()
    }
  } catch (e: unknown) {
    verifyResult.value = { valid: false, message: `Failed to apply: ${formatApiError(e)}` }
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
      verifyResult.value = { valid: false, message: `Failed to remove: ${formatApiError(err)}` }
    } else {
      removeDialogOpen.value = false
      await planStore.fetchPlan()
      await loadAll()
    }
  } catch (e: unknown) {
    verifyResult.value = { valid: false, message: `Failed to remove: ${formatApiError(e)}` }
  } finally {
    removing.value = false
  }
}

onMounted(() => {
  planStore.fetchPlan()
  loadAll()
})
</script>

