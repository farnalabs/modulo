<template>
  <div data-theme="agent" class="page-narrow">
    <PageHeader :title="$t('views.SettingsEmailView.email_settings')" :subtitle="$t('views.SettingsEmailView.configure_smtp_provider_for_transactional_emails')" />

    <FeatureGate feature-name="email_config" required-tier="team" show-disabled>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="loadError" :message="loadError" :on-retry="loadSettings" />

      <div v-else class="space-y-6">
        <div class="rounded-lg border bg-card shadow-sm">
          <div class="p-6 space-y-4">
            <div>
              <label for="settingsemailview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsEmailView.smtp_host') }}</label>
              <input id="settingsemailview-field-5"
                v-model="form.smtp_host"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                :placeholder="$t('views.SettingsEmailView.smtp_host_placeholder')"
              />
            </div>
            <div>
              <label for="settingsemailview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsEmailView.smtp_port') }}</label>
              <input id="settingsemailview-field-4"
                v-model.number="form.smtp_port"
                type="number"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                placeholder="587"
              />
            </div>
            <div>
              <label for="settingsemailview-field-3" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsEmailView.smtp_username') }}</label>
              <input id="settingsemailview-field-3"
                v-model="form.smtp_username"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                :placeholder="$t('views.SettingsEmailView.smtp_username_placeholder')"
              />
            </div>
            <div>
              <label for="settingsemailview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsEmailView.smtp_password') }}</label>
              <input id="settingsemailview-field-2"
                v-model="form.smtp_password"
                type="password"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                :placeholder="$t('views.SettingsEmailView.smtp_password_placeholder')"
              />
            </div>
            <div>
              <label for="settingsemailview-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.SettingsEmailView.email_from') }}</label>
              <input id="settingsemailview-field-1"
                v-model="form.email_from"
                type="email"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                :placeholder="$t('views.SettingsEmailView.email_from_placeholder')"
              />
            </div>

            <div class="flex items-center gap-3 pt-2">
              <Button
                type="button"
                variant="default"
                :disabled="saving"
                @click="saveSettings"
              >
                {{ saving ? $t('views.SettingsEmailView.saving') : $t('views.SettingsEmailView.save') }}
              </Button>
              <button
                type="button"
                :disabled="testing"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
                @click="testSettings"
              >
                {{ testing ? $t('views.SettingsEmailView.testing') : $t('views.SettingsEmailView.test_email') }}
              </button>
            </div>
          </div>
        </div>

        <div
          v-if="successMessage"
          class="rounded-lg border border-success/50 bg-success/10 p-3 text-sm text-success"
        >
          {{ successMessage }}
        </div>
        <div
          v-if="errorMessage"
          class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
        >
          {{ errorMessage }}
        </div>
      </div>
    </FeatureGate>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError, type ProblemDetail } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'
import { api } from '../lib/api/client'
import FeatureGate from '../components/FeatureGate.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { Button } from '@/components/ui/button'

interface EmailForm {
  smtp_host: string
  smtp_port: number
  smtp_username: string
  smtp_password: string
  email_from: string
}

const planStore = usePlanStore()

const { loading, error: loadError, load: loadSettings } = useDataFetch(
  async () => {
    const orgId = planStore.orgId
    if (!orgId) return { error: { detail: 'Organisation ID not available' } }
    const res = await (api as any).GET('/api/v1/admin/org/{org_id}/email-settings', {
      params: { path: { org_id: orgId } },
    })
    if (res.data) {
      form.smtp_host = res.data.smtp_host || ''
      form.smtp_port = res.data.smtp_port || 587
      form.smtp_username = res.data.smtp_username || ''
      form.smtp_password = ''
      form.email_from = res.data.email_from || ''
    }
    return res
  },
  { immediate: false }
)
const saving = ref(false)
const testing = ref(false)
const successMessage = ref<string | null>(null)
const errorMessage = ref<string | null>(null)
let clearTimeoutId: ReturnType<typeof setTimeout> | null = null

const form = reactive<EmailForm>({
  smtp_host: '',
  smtp_port: 587,
  smtp_username: '',
  smtp_password: '',
  email_from: '',
})

function getOrgId(): string | null {
  return planStore.orgId
}

async function saveSettings() {
  saving.value = true
  errorMessage.value = null
  successMessage.value = null
  try {
    const orgId = getOrgId()
    if (!orgId) {
      errorMessage.value = 'Could not determine organisation'
      return
    }
    const { error: err } = await (api as any).PUT('/api/v1/admin/org/{org_id}/email-settings', {
      params: { path: { org_id: orgId } },
      body: {
        smtp_host: form.smtp_host,
        smtp_port: form.smtp_port,
        smtp_username: form.smtp_username,
        smtp_password: form.smtp_password,
        email_from: form.email_from,
      },
    })
    if (err) {
      errorMessage.value = err && typeof err === 'object' && 'detail' in err
        ? `Save failed: ${(err as ProblemDetail).detail}`
        : `Save failed: ${formatApiError(err)}`
    } else {
      successMessage.value = 'Email settings saved.'
      if (clearTimeoutId) clearTimeout(clearTimeoutId)
      clearTimeoutId = setTimeout(() => { successMessage.value = null }, 3000)
    }
  } catch (e: unknown) {
    errorMessage.value = `Save failed: ${formatApiError(e)}`
  } finally {
    saving.value = false
  }
}

async function testSettings() {
  testing.value = true
  errorMessage.value = null
  successMessage.value = null
  try {
    const orgId = getOrgId()
    if (!orgId) return
    const { data, error: err } = await (api as any).POST('/api/v1/admin/org/{org_id}/email-settings/test', {
      params: { path: { org_id: orgId } },
      body: { to: 'test@example.com' },
    })
    if (err) {
      errorMessage.value = err && typeof err === 'object' && 'detail' in err
        ? `Test failed: ${(err as ProblemDetail).detail}`
        : `Test failed: ${formatApiError(err)}`
    } else if (data) {
      if (data.ok) {
        successMessage.value = data.message || 'Test email sent successfully.'
        if (clearTimeoutId) clearTimeout(clearTimeoutId)
        clearTimeoutId = setTimeout(() => { successMessage.value = null }, 5000)
      } else {
        errorMessage.value = data.message || 'Test failed.'
      }
    }
  } catch (e: unknown) {
    errorMessage.value = `Test failed: ${formatApiError(e)}`
  } finally {
    testing.value = false
  }
}

onBeforeUnmount(() => {
  if (clearTimeoutId) clearTimeout(clearTimeoutId)
})

onMounted(async () => {
  const promise = planStore.fetchPlan()
  if (promise) await promise
  if (planStore.featureEnabled('email_config')) {
    loadSettings()
  }
})
</script>
