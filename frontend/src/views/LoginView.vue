<template>
  <div class="relative mx-auto flex min-h-screen max-w-md items-center justify-center p-6">
    <div
      class="pointer-events-none fixed inset-0 -z-10"
      style="background-image: radial-gradient(circle at 1px 1px, var(--dot-color) 1px, transparent 0); background-size: 32px 32px;"
    />

    <div class="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[500px] h-[500px] rounded-full bg-primary/3 blur-3xl pointer-events-none" />

    <div class="relative w-full space-y-6">
      <div class="text-center">
        <div class="mb-4 flex justify-center">
          <div class="flex h-14 w-14 items-center justify-center rounded-xl bg-primary/10 border border-primary/20">
            <svg width="32" height="32" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Modulo logo">
              <g stroke="#00FFD1" stroke-width="7" fill="none" stroke-linejoin="round" stroke-linecap="round">
                <line x1="30" y1="84.64" x2="70" y2="15.36" />
                <polygon points="36,28 31,36.66 21,36.66 16,28 21,19.34 31,19.34" />
                <polygon points="84,72 79,80.66 69,80.66 64,72 69,63.34 79,63.34" />
              </g>
            </svg>
          </div>
        </div>
        <h1 class="text-3xl font-bold tracking-tight">Modulo</h1>
        <p class="mt-1 text-muted-foreground">Governed orchestration for your agentic SDLC</p>
      </div>

      <div v-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
        {{ error }}
      </div>

      <form @submit.prevent="login" class="rounded-xl border bg-card p-6 space-y-4 shadow-sm">
        <div class="space-y-2">
          <label class="text-sm font-medium">Email</label>
          <input
            v-model="email"
            type="text"
            class="input-teal w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            placeholder="admin@example.com"
            required
            data-testid="login-email"
          />
        </div>
        <div class="space-y-2">
          <label class="text-sm font-medium">Password</label>
          <input
            v-model="password"
            type="password"
            class="input-teal w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            placeholder="Enter your password"
            required
            data-testid="login-password"
          />
        </div>
        <button
          type="submit"
          :disabled="loading"
          class="btn-glow w-full rounded-md bg-primary px-4 py-2.5 text-sm font-semibold text-primary-foreground border border-primary/30 hover:border-primary/60 hover:brightness-110 disabled:opacity-50 transition-all duration-150"
          data-testid="login-submit"
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
      error.value = body.detail || res.statusText
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
