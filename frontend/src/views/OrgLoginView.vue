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
        <p v-if="orgName" class="mt-1 text-sm text-muted-foreground">{{ $t('views.LoginView.sign_in_to_org', { orgName }) }}</p>
        <p class="mt-1 text-muted-foreground">{{ $t('views.LoginView.agent_governance_for_your_agentic_sdlc') }}</p>
        <button
          type="button"
          class="mt-2 text-xs text-muted-foreground underline underline-offset-4 transition-colors hover:text-foreground"
          data-testid="org-login-change-org"
          @click="handleChangeOrg"
        >
          {{ $t('views.OrgLoginView.change_organization') }}
        </button>
      </div>

      <div v-if="loading" class="text-center text-muted-foreground" data-testid="org-login-loading">
        {{ $t('common.loading') }}
      </div>

      <div v-else-if="notFound" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive" role="alert" aria-live="assertive" data-testid="org-login-not-found">
        {{ $t('views.OrgLoginView.organisation_not_found') }}
      </div>

      <template v-else>
        <div v-if="loginError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive" role="alert" aria-live="assertive" data-testid="org-login-error">
          {{ loginError }}
        </div>

        <form v-if="passwordEnabled" @submit.prevent="handleLogin" class="rounded-xl border bg-card p-6 space-y-4 shadow-sm">
          <div class="space-y-2">
            <label for="org-login-email" class="text-sm font-medium">{{ $t('common.email') }}</label>
            <input id="org-login-email"
              v-model="email"
              type="text"
              class="input-teal w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              placeholder="admin@example.com"
              required
              data-testid="org-login-email"
            />
          </div>
          <div class="space-y-2">
            <div class="flex items-center justify-between gap-2">
              <label for="org-login-password" class="text-sm font-medium">{{ $t('common.password') }}</label>
              <span
                v-if="lastUsedMethod === 'password'"
                class="inline-flex shrink-0 items-center rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
                data-testid="org-login-last-used-password"
              >{{ $t('views.LoginView.used_last_time') }}</span>
            </div>
            <input id="org-login-password"
              v-model="password"
              type="password"
              class="input-teal w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.LoginView.enter_your_password')"
              required
              data-testid="org-login-password"
            />
          </div>
          <Button type="submit" :disabled="loadingLogin" class="w-full border-primary/30 hover:border-primary/60 px-4 py-2.5" data-testid="org-login-submit">
            {{ loadingLogin ? $t('common.signing_in') : $t('common.sign_in') }}
          </Button>
        </form>

        <div v-if="providers.length > 0 || samlEnabled" class="space-y-3" data-testid="org-login-sso-section">
          <div class="flex items-center gap-3 text-xs text-muted-foreground">
            <span class="h-px flex-1 bg-border" />
            <span>{{ $t('views.LoginView.or_continue_with') }}</span>
            <span class="h-px flex-1 bg-border" />
          </div>
          <div class="space-y-2">
            <a
              v-for="provider in providers"
              :key="provider.provider_id"
              :href="provider.type === 'saml'
                ? `/api/v1/auth/saml/${provider.provider_id}/login`
                : `/api/v1/auth/oidc/${provider.provider_id}/login`"
              class="flex w-full items-center justify-center gap-2 rounded-md border border-input bg-background px-4 py-2 text-sm text-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
              :data-testid="`org-login-sso-${provider.provider_id}`"
              @click="rememberSsoMethod(provider.provider_id)"
            >
              <SsoBrandMark :preset="provider.type === 'saml' ? 'custom' : (provider.preset ?? 'custom')" />
              {{ $t('views.LoginView.sign_in_with', { provider: provider.display_name || provider.provider_id }) }}
              <span
                v-if="lastUsedMethod === provider.provider_id"
                class="inline-flex shrink-0 items-center rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
                data-testid="org-login-last-used-sso"
              >{{ $t('views.LoginView.used_last_time') }}</span>
            </a>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import { useMutation } from '../composables/useMutation'
import { useLoginPrefs } from '../composables/useLoginPrefs'
import { setAccessToken } from '../lib/api/client'
import { setMustChangePassword } from '../lib/mustChangePassword'
import type { components } from '../lib/api/schema'
import SsoBrandMark from '../components/SsoBrandMark.vue'

type OrgLoginProviderInfo = components['schemas']['OrgLoginProviderInfo']

const route = useRoute()
const router = useRouter()
const slug = route.params.slug as string

// --- Login preferences (last org slug + last-used method) ---
const loginPrefs = useLoginPrefs()
const lastUsedMethod = ref<string | null>(loginPrefs.getLastMethod())

const orgName = ref('')
const providers = ref<OrgLoginProviderInfo[]>([])
const passwordEnabled = ref(true)
const samlEnabled = ref(false)
const loading = ref(true)
const notFound = ref(false)

const email = ref('')
const password = ref('')

async function fetchOrgLogin() {
  try {
    const res = await fetch(`/api/v1/auth/org-login/${encodeURIComponent(slug)}`)
    if (!res.ok) {
      notFound.value = true
      loading.value = false
      return
    }
    const data = await res.json()
    orgName.value = data.org?.name || slug
    providers.value = data.providers ?? []
    passwordEnabled.value = data.password_enabled !== false
    samlEnabled.value = Boolean(data.saml)
    loading.value = false
  } catch {
    notFound.value = true
    loading.value = false
  }
}

onMounted(fetchOrgLogin)

/**
 * "change organization": forget the remembered slug and return to the root
 * slug-entry point. Works from any state (including not-found) so a stale
 * stored slug can always be replaced. Storage failures are swallowed by
 * useLoginPrefs — navigation still proceeds.
 */
function handleChangeOrg() {
  loginPrefs.clearLastOrgSlug()
  router.push('/login')
}

/**
 * Record the SSO provider the user is activating for the next visit's
 * "used last time" tag. Success is handed off at /auth/callback (outside
 * this view) — best-effort at activation. Storage failures are swallowed.
 */
function rememberSsoMethod(method: string) {
  loginPrefs.setLastMethod(method)
  lastUsedMethod.value = method
}

function handleLogin() {
  login().catch(() => {})
}

const { loading: loadingLogin, error: loginError, mutate: login } = useMutation(async () => {
  const res = await fetch('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: email.value, password: password.value, org_slug: slug }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || res.statusText)
  }
  const data = await res.json()
  setAccessToken(data.access_token)
  setMustChangePassword(data.must_change_password === true)
  // Successful login for this org — remember slug + method for next visit.
  // Best-effort: a storage failure must never block a successful login.
  loginPrefs.setLastOrgSlug(slug)
  loginPrefs.setLastMethod('password')
  lastUsedMethod.value = 'password'
  router.push('/')
  return data
})
</script>
