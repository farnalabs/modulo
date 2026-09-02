<template>
  <div class="relative mx-auto flex min-h-screen max-w-md items-center justify-center overflow-x-hidden p-6">
    <div
      class="pointer-events-none fixed inset-0 -z-10"
      style="background-image: radial-gradient(circle at 1px 1px, var(--dot-color) 1px, transparent 0); background-size: 32px 32px;"
    />

    <div class="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[500px] h-[500px] rounded-full bg-primary/3 blur-3xl pointer-events-none" />

    <div class="relative w-full space-y-6">
      <div class="text-center">
        <div class="mb-4 flex justify-center">
          <div class="flex h-14 w-14 items-center justify-center rounded-xl bg-primary/10 border border-primary/20">
            <svg width="32" height="32" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" :aria-label="$t('components.LogoMark.modulo_logo')">
              <g stroke="#00FFD1" stroke-width="7" fill="none" stroke-linejoin="round" stroke-linecap="round">
                <line x1="30" y1="84.64" x2="70" y2="15.36" />
                <polygon points="36,28 31,36.66 21,36.66 16,28 21,19.34 31,19.34" />
                <polygon points="84,72 79,80.66 69,80.66 64,72 69,63.34 79,63.34" />
              </g>
            </svg>
          </div>
        </div>
        <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.LoginView.modulo') }}</h1>
        <p class="mt-1 text-muted-foreground">{{ $t('views.LoginView.agent_governance_for_your_agentic_sdlc') }}</p>
      </div>

      <div v-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
        {{ error }}
      </div>

      <form @submit.prevent="() => login()" class="rounded-xl border bg-card p-6 space-y-4 shadow-sm">
        <div class="space-y-2">
          <label for="loginview-field-2" class="text-sm font-medium">{{ $t('common.email') }}</label>
          <input id="loginview-field-2"
            v-model="email"
            type="text"
            class="input-teal w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            placeholder="admin@example.com"
            required
            data-testid="login-email"
          />
        </div>
        <div class="space-y-2">
          <label for="loginview-field-1" class="text-sm font-medium">{{ $t('common.password') }}</label>
          <input id="loginview-field-1"
            v-model="password"
            type="password"
            class="input-teal w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('views.LoginView.enter_your_password')"
            required
            data-testid="login-password"
          />
        </div>
        <Button type="submit" :disabled="loading" class="w-full border-primary/30 hover:border-primary/60 px-4 py-2.5" data-testid="login-submit">
          {{ loading ? $t('common.signing_in') : $t('common.sign_in') }}
        </Button>
      </form>

      <div v-if="ssoState === 'available'" class="space-y-3" data-testid="login-sso-section">
        <div class="flex items-center gap-3 text-xs text-muted-foreground">
          <span class="h-px flex-1 bg-border" />
          <span>{{ $t('views.LoginView.or_continue_with') }}</span>
          <span class="h-px flex-1 bg-border" />
        </div>
        <div class="space-y-2">
          <a
            v-for="provider in oidcProviders"
            :key="provider.provider_id"
            :href="`/api/v1/auth/oidc/${provider.provider_id}/login`"
            class="flex w-full items-center justify-center gap-2 rounded-md border border-input bg-background px-4 py-2 text-sm text-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            :data-testid="`login-sso-oidc-${provider.provider_id}`"
          >
            {{ provider.provider_id }}
          </a>
          <a
            v-if="samlEnabled"
            href="/api/v1/auth/saml/login"
            class="flex w-full items-center justify-center gap-2 rounded-md border border-input bg-background px-4 py-2 text-sm text-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            data-testid="login-sso-saml"
          >
            SAML
          </a>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import { useMutation } from '../composables/useMutation'
import { setAccessToken, setRefreshToken } from '../lib/api/client'
import { setMustChangePassword } from '../lib/mustChangePassword'

interface SsoProviderInfo {
  provider_id: string
}

interface SsoProvidersResponse {
  oidc: SsoProviderInfo[]
  saml: boolean
}

// SSO is a licensed (team-tier) surface; the login page renders its provider
// buttons only when the instance advertises configured providers. Since #2325
// the endpoint answers 200 with an empty providers list when SSO is disabled
// (oidc: [], saml: false) — that, any non-200, or a fetch failure keeps the
// page on password login.
const ssoState = ref<'unknown' | 'available' | 'unavailable'>('unknown')
const oidcProviders = ref<SsoProviderInfo[]>([])
const samlEnabled = ref(false)

async function discoverSsoProviders() {
  try {
    const res = await fetch('/api/v1/auth/sso/providers')
    if (!res.ok) {
      ssoState.value = 'unavailable'
      return
    }
    const data = (await res.json()) as SsoProvidersResponse
    oidcProviders.value = data.oidc ?? []
    samlEnabled.value = Boolean(data.saml)
    ssoState.value = oidcProviders.value.length > 0 || samlEnabled.value ? 'available' : 'unavailable'
  } catch {
    ssoState.value = 'unavailable'
  }
}

onMounted(discoverSsoProviders)

const router = useRouter()
const email = ref('')
const password = ref('')

const { loading, error, mutate: login } = useMutation(async () => {
  const res = await fetch('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: email.value, password: password.value }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || res.statusText)
  }
  const data = await res.json()
  setAccessToken(data.access_token)
  if (data.refresh_token) setRefreshToken(data.refresh_token)
  // FAR-460: sync the must-change-password gate from every manual login
  // response — true forces the full-screen change-password view; false clears
  // any stale flag so a different account is never trapped behind the gate.
  setMustChangePassword(data.must_change_password === true)
  router.push('/')
  return data
})
</script>
