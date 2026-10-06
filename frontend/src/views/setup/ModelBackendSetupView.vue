<template>
  <div class="flex min-h-screen items-center justify-center bg-background p-4">
    <div class="w-full max-w-md rounded-lg border p-6 shadow-sm">
      <PageHeader :title="t('views.ModelBackendSetupView.complete_setup_title')" :subtitle="t('views.ModelBackendSetupView.complete_setup_subtitle')" />

      <div v-if="success" class="space-y-4">
        <div class="rounded-md bg-green-50 p-3 text-sm text-green-800">
          {{ t('views.ModelBackendSetupView.backend_active', { name: backendName }) }}
        </div>
        <Button severity="secondary" outlined class="w-full" data-testid="model-backend-setup-view-backends" @click="router.push('/admin/model-backends')">
          {{ t('views.ModelBackendSetupView.view_model_backends') }}
        </Button>
      </div>

      <div v-else-if="!token" class="space-y-4">
        <div class="rounded-md bg-amber-50 p-3 text-sm text-amber-800">
          {{ t('views.ModelBackendSetupView.missing_token') }}
        </div>
      </div>

      <form v-else data-testid="model-backend-setup-form" @submit.prevent="() => submit()" class="space-y-4">
        <div>
          <label for="model-backend-setup-api-key" class="mb-1 block text-sm font-medium">{{ t('views.ModelBackendSetupView.api_key') }}</label>
          <InputText id="model-backend-setup-api-key"
            v-model="apiKey"
            type="password"
            :placeholder="t('views.ModelBackendSetupView.api_key_placeholder')"
            :disabled="loading"
            data-testid="model-backend-setup-api-key"
            class="w-full"
          />
        </div>

        <p v-if="error" class="text-sm text-red-600">{{ error }}</p>

        <Button type="submit" :disabled="loading || !apiKey.trim()" class="w-full" data-testid="model-backend-setup-submit">
          {{ loading ? t('views.ModelBackendSetupView.saving') : t('views.ModelBackendSetupView.complete_setup_action') }}
        </Button>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import PageHeader from '../../components/shared/PageHeader.vue'
import { useApi } from '../../composables/useApi'
import { useMutation } from '../../composables/useMutation'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const { post } = useApi()

// The one-time setup token is delivered in the URL FRAGMENT (#token=...) — never
// the query string — so it is not sent to the server nor leaked via Referer/access
// logs. Read it from the hash, then strip it from the address bar / browser history
// so it does not linger after the handoff is consumed.
const token = parseFragmentToken(window.location.hash)
if (token) {
  history.replaceState(null, '', window.location.pathname + window.location.search)
}

const backendId = route.params.id as string
const apiKey = ref('')
const success = ref(false)
const backendName = ref('')

const { loading, error, mutate: submit } = useMutation(async () => {
  if (!apiKey.value.trim()) return
  try {
    const resp = await post<{ status: string; backend_id: string; name: string }>(
      `/api/v1/model-backends/${backendId}/complete-setup`,
      { token, api_key: apiKey.value }
    )
    backendName.value = resp.name
    success.value = true
    return resp
  } catch (e: unknown) {
    const detail = typeof e === 'object' && e !== null
      ? String((e as Record<string, unknown>).detail ?? (e as Record<string, unknown>).message ?? '')
      : ''
    if (detail.includes('invalid_token')) {
      throw new Error(t('views.ModelBackendSetupView.setup_failed_expired_token'))
    } else if (detail.includes('backend_not_found')) {
      throw new Error(t('views.ModelBackendSetupView.setup_failed_backend_not_found'))
    }
    throw new Error(t('views.ModelBackendSetupView.setup_failed_generic'))
  }
})

function parseFragmentToken(hash: string): string | null {
  if (!hash || hash.length < 2) return null
  const t = new URLSearchParams(hash.slice(1)).get('token')
  return t ?? null
}
</script>
