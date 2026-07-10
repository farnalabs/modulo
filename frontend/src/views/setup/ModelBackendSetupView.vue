<template>
  <div class="flex min-h-screen items-center justify-center bg-background p-4">
    <div class="w-full max-w-md rounded-lg border p-6 shadow-sm">
      <h1 class="mb-2 text-xl font-semibold">Complete Model Backend Setup</h1>
      <p class="mb-6 text-sm text-muted-foreground">
        A model backend was created via MCP. Paste the API key below to complete setup.
      </p>

      <div v-if="success" class="space-y-4">
        <div class="rounded-md bg-green-50 p-3 text-sm text-green-800">
          Backend "{{ backendName }}" is now active.
        </div>
        <Button variant="outline" class="w-full" @click="router.push('/admin/model-backends')">
          View Model Backends
        </Button>
      </div>

      <form v-else @submit.prevent="submit" class="space-y-4">
        <div>
          <label class="mb-1 block text-sm font-medium">API Key</label>
          <Input
            v-model="apiKey"
            type="password"
            placeholder="sk-..."
            :disabled="loading"
            class="w-full"
          />
        </div>

        <p v-if="error" class="text-sm text-red-600">{{ error }}</p>

        <Button type="submit" :disabled="loading || !apiKey.trim()" class="w-full">
          {{ loading ? 'Saving...' : 'Complete Setup' }}
        </Button>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '../../composables/useApi'
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'

const route = useRoute()
const router = useRouter()
const { post } = useApi()

const backendId = route.params.id as string
const token = route.query.token as string
const apiKey = ref('')
const loading = ref(false)
const success = ref(false)
const backendName = ref('')
const error = ref('')

async function submit() {
  if (!apiKey.value.trim()) return
  loading.value = true
  error.value = ''
  try {
    const resp = await post<{ status: string; backend_id: string; name: string }>(
      `/model-backends/${backendId}/complete-setup`,
      { token, api_key: apiKey.value }
    )
    backendName.value = resp.name
    success.value = true
  } catch (e: any) {
    const detail = e?.detail || e?.message || ''
    if (detail?.includes('invalid_token')) {
      error.value = 'Setup link expired or already used. Re-run the MCP command to generate a new setup URL.'
    } else if (detail?.includes('backend_not_found')) {
      error.value = 'Model backend not found. It may have been deleted.'
    } else {
      error.value = 'Setup failed. Please try again.'
    }
  } finally {
    loading.value = false
  }
}
</script>
