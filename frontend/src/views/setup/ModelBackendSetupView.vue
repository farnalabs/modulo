<template>
  <div class="flex min-h-screen items-center justify-center bg-background p-4">
    <div class="w-full max-w-md rounded-lg border p-6 shadow-sm">
      <PageHeader title="Complete Model Backend Setup" subtitle="A model backend was created via MCP. Paste the API key below to complete setup." />

      <div v-if="success" class="space-y-4">
        <div class="rounded-md bg-green-50 p-3 text-sm text-green-800">
          Backend "{{ backendName }}" is now active.
        </div>
        <Button variant="outline" class="w-full" @click="router.push('/admin/model-backends')">
          View Model Backends
        </Button>
      </div>

      <form v-else @submit.prevent="() => submit()" class="space-y-4">
        <div>
          <span class="mb-1 block text-sm font-medium">API Key</span>
          <Input aria-label="Form control"
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
import PageHeader from '../../components/shared/PageHeader.vue'
import { useApi } from '../../composables/useApi'
import { useMutation } from '../../composables/useMutation'
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'

const route = useRoute()
const router = useRouter()
const { post } = useApi()

const backendId = route.params.id as string
const token = route.query.token as string
const apiKey = ref('')
const success = ref(false)
const backendName = ref('')

const { loading, error, mutate: submit } = useMutation(async () => {
  if (!apiKey.value.trim()) return
  try {
    const resp = await post<{ status: string; backend_id: string; name: string }>(
      `/model-backends/${backendId}/complete-setup`,
      { token, api_key: apiKey.value }
    )
    backendName.value = resp.name
    success.value = true
    return resp
  } catch (e: any) {
    const detail = e?.detail || e?.message || ''
    if (detail.includes('invalid_token')) {
      throw new Error('Setup link expired or already used. Re-run the MCP command to generate a new setup URL.')
    } else if (detail.includes('backend_not_found')) {
      throw new Error('Model backend not found. It may have been deleted.')
    }
    throw new Error('Setup failed. Please try again.')
  }
})
</script>
