<template>
  <div class="mx-auto flex min-h-screen max-w-md items-center justify-center p-6">
    <div class="w-full space-y-6">
      <div class="text-center">
        <h1 class="text-3xl font-bold tracking-tight">Modulo</h1>
        <p class="mt-1 text-muted-foreground">Self-hosted agentic SDLC platform</p>
      </div>

      <div v-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
        {{ error }}
      </div>

      <form @submit.prevent="login" class="space-y-4">
        <div class="space-y-2">
          <label class="text-sm font-medium">Email</label>
          <input
            v-model="email"
            type="text"
            class="w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="admin@example.com"
            required
          />
        </div>
        <div class="space-y-2">
          <label class="text-sm font-medium">Password</label>
          <input
            v-model="password"
            type="password"
            class="w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            placeholder="Enter your password"
            required
          />
        </div>
        <button
          type="submit"
          :disabled="loading"
          class="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {{ loading ? 'Signing in...' : 'Sign in' }}
        </button>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { setAccessToken } from '../lib/api/client'

const router = useRouter()
const email = ref('')
const password = ref('')
const loading = ref(false)
const error = ref<string | null>(null)

async function login() {
  loading.value = true
  error.value = null
  try {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email.value, password: password.value }),
    })
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      error.value = `Login failed: ${body.error?.message || res.statusText}`
      return
    }
    const data = await res.json()
    setAccessToken(data.access_token)
    router.push('/')
  } catch (e: unknown) {
    error.value = `Login failed: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}
</script>
