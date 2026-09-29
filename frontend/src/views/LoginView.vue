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
        <p v-if="displayOrgName" class="mt-1 text-sm text-muted-foreground">{{ $t('views.LoginView.sign_in_to_org', { orgName: displayOrgName }) }}</p>
        <p class="mt-1 text-muted-foreground">{{ $t('views.LoginView.agent_governance_for_your_agentic_sdlc') }}</p>
      </div>

      <div v-if="contextLoading || redirectingToOrg" class="text-center text-muted-foreground" data-testid="login-context-loading">
        {{ $t('common.loading') }}
      </div>

      <!-- Multi-org entry step: enter slug to navigate to org-specific login -->
      <template v-else-if="multiOrg">
        <form @submit.prevent="handleOrgEntry" class="rounded-xl border bg-card p-6 space-y-4 shadow-sm">
          <div class="space-y-2">
            <label for="org-slug-input" class="text-sm font-medium">{{ $t('views.OrgLoginView.organisation_slug_label') }}</label>
            <input id="org-slug-input"
              v-model="orgSlugInput"
              type="text"
              class="input-teal w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.OrgLoginView.organisation_slug_placeholder')"
              required
              data-testid="login-org-slug"
              aria-describedby="org-slug-hint"
            />
            <p id="org-slug-hint" class="text-xs text-muted-foreground">{{ $t('views.OrgLoginView.organisation_slug_hint') }}</p>
          </div>
          <Button type="submit" class="w-full border-primary/30 hover:border-primary/60 px-4 py-2.5" data-testid="login-org-entry-submit">
            {{ $t('views.OrgLoginView.continue') }}
          </Button>
        </form>
      </template>

      <!-- Single-org: render login directly (auto-skipped from login-context) -->
      <template v-else>
        <div v-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive" role="alert" aria-live="assertive">
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
            <div class="flex items-center justify-between gap-2">
              <label for="loginview-field-1" class="text-sm font-medium">{{ $t('common.password') }}</label>
              <span
                v-if="lastUsedMethod === 'password'"
                class="inline-flex shrink-0 items-center rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
                data-testid="login-last-used-password"
              >{{ $t('views.LoginView.used_last_time') }}</span>
            </div>
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
              @click="rememberSsoMethod(provider.provider_id)"
            >
              <SsoBrandMark :preset="provider.preset ?? 'custom'" />
              {{ $t('views.LoginView.sign_in_with', { provider: provider.display_name || provider.provider_id }) }}
              <span
                v-if="lastUsedMethod === provider.provider_id"
                class="inline-flex shrink-0 items-center rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
                data-testid="login-last-used-sso"
              >{{ $t('views.LoginView.used_last_time') }}</span>
            </a>
            <a
              v-if="samlEnabled"
              href="/api/v1/auth/saml/login"
              class="flex w-full items-center justify-center gap-2 rounded-md border border-input bg-background px-4 py-2 text-sm text-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
              data-testid="login-sso-saml"
              @click="rememberSsoMethod('saml')"
            >
              SAML
              <span
                v-if="lastUsedMethod === 'saml'"
                class="inline-flex shrink-0 items-center rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
                data-testid="login-last-used-sso"
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
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import { useMutation } from '../composables/useMutation'
import { useLoginPrefs } from '../composables/useLoginPrefs'
import { withTimeout } from '../lib/asyncUtils'
import { setAccessToken } from '../lib/api/client'
import { setMustChangePassword } from '../lib/mustChangePassword'
import type { components } from '../lib/api/schema'
import SsoBrandMark from '../components/SsoBrandMark.vue'

interface SsoProviderInfo {
  provider_id: string
  display_name: string
  preset?: string | null
}

interface SsoProvidersResponse {
  oidc: SsoProviderInfo[]
  saml: boolean
}

type OrgInfo = components['schemas']['OrgInfo']

// --- Login preferences (last org slug + last-used method) ---
const loginPrefs = useLoginPrefs()
const lastUsedMethod = ref<string | null>(loginPrefs.getLastMethod())

// Budgets for the two anonymous pre-auth reads. Only login-context GATES the
// credential form; SSO discovery is a best-effort enrichment that must never
// delay the form. Neither call may strand the page: if login-context never
// settles, the timeout rejects the awaited promise and the form still renders.
// Observed on staging under load — a hung login-context left the page on the
// "Loading" state forever, so no credential form ever rendered and every
// @regression test that signs in failed (and retried) until the job hit its
// 90-minute timeout. Fail over to the direct login form instead.
const LOGIN_CONTEXT_TIMEOUT_MS = 8_000
const SSO_DISCOVERY_TIMEOUT_MS = 8_000

// --- Login context state ---
const contextLoading = ref(true)
const multiOrg = ref(false)
const singleOrg = ref<OrgInfo | null>(null)
// True while auto-redirecting to a remembered /login/:slug — keeps the
// loading state up (never flashes the slug-entry form) until navigation.
const redirectingToOrg = ref(false)

// --- Org entry state (multi-org path) ---
const orgSlugInput = ref('')

// --- SSO state (single-org fallback) ---
const ssoState = ref<'unknown' | 'available' | 'unavailable'>('unknown')
const oidcProviders = ref<SsoProviderInfo[]>([])
const samlEnabled = ref(false)

// Computed display name for single-org auto-skip
const displayOrgName = ref('')

async function fetchLoginContext() {
  try {
    // Bound the body read, not just the response headers: a server that sends
    // headers and then stalls the JSON body would otherwise leave `res.json()`
    // outside the timeout window and strand the page just like a hung request.
    // The whole fetch→parse sequence runs inside the timeout so the fallback
    // below is always reached.
    const result = await withTimeout(
      fetch('/api/v1/auth/login-context').then(async (res) => ({
        ok: res.ok,
        data: res.ok ? await res.json() : null,
      })),
      LOGIN_CONTEXT_TIMEOUT_MS,
      'login-context',
    )
    if (!result.ok) {
      // login-context is unavailable (e.g. a transient 429 from the anonymous
      // GET rate limit). Fall back to the single-org direct login.
      // SSO discovery is started but NOT awaited: the credential form must not
      // wait for it (see discoverSsoProviders for the decoupling rationale).
      void discoverSsoProviders()
      return
    }
    const data = result.data
    if (!data.multi_org && data.org) {
      // Single org: auto-skip to direct login (preserve existing UX)
      singleOrg.value = data.org
      multiOrg.value = false
      displayOrgName.value = data.org.name || ''
      // Discover SSO providers for this org using the old global endpoint
      // as a fallback. The org-login endpoint is used for /login/:slug.
      // Not awaited — the form renders from `contextLoading=false` below and
      // the SSO section appears reactively when this resolves.
      void discoverSsoProviders()
    } else {
      // Multiple orgs: jump straight to a previously-used org's login when
      // one is remembered; otherwise show the slug entry step.
      const rememberedSlug = loginPrefs.getLastOrgSlug()
      if (rememberedSlug) {
        redirectingToOrg.value = true
        window.location.href = `/login/${encodeURIComponent(rememberedSlug)}`
        return
      }
      multiOrg.value = true
      singleOrg.value = null
    }
  } catch {
    // Network failure resolving the login context: same fallback as a non-ok
    // response — render the direct login; SSO discovery is best-effort and
    // must not delay the form.
    void discoverSsoProviders()
  } finally {
    // The credential form is gated ONLY on login-context (or its timeout). SSO
    // discovery is deliberately excluded from this critical path: awaiting it
    // serialised two 8s budgets (login-context then sso-providers), so a
    // doubly-slow pre-auth path took up to 16s to render the form — longer
    // than the E2E credential-form guard (10s), which made every signing-in
    // @regression test fail. Rendering the form as soon as org context
    // resolves keeps the worst case at LOGIN_CONTEXT_TIMEOUT_MS.
    contextLoading.value = false
  }
}

async function discoverSsoProviders() {
  try {
    // Read the body inside the timeout window so a stalled JSON body cannot
    // hang the page after headers have already arrived.
    const data = await withTimeout(
      fetch('/api/v1/auth/sso/providers').then(async (res) =>
        res.ok ? ((await res.json()) as SsoProvidersResponse) : null,
      ),
      SSO_DISCOVERY_TIMEOUT_MS,
      'sso-providers',
    )
    if (!data) {
      ssoState.value = 'unavailable'
      return
    }
    oidcProviders.value = data.oidc ?? []
    samlEnabled.value = Boolean(data.saml)
    ssoState.value = oidcProviders.value.length > 0 || samlEnabled.value ? 'available' : 'unavailable'
  } catch {
    ssoState.value = 'unavailable'
  }
}

onMounted(fetchLoginContext)

// --- Org entry handler ---
function handleOrgEntry() {
  const slug = orgSlugInput.value.trim()
  if (!slug) return
  // Remember the slug so the next multi-org /login visit skips the entry
  // step. Best-effort: a storage failure must never block navigation.
  loginPrefs.setLastOrgSlug(slug)
  // Navigate to /login/:slug — the OrgLoginView will fetch the org's data.
  // If the slug is invalid, OrgLoginView shows a generic not-found error
  // (never reveals whether the org exists — tenancy boundary).
  window.location.href = `/login/${encodeURIComponent(slug)}`
}

/**
 * Record the SSO method the user is activating so the next visit can tag it
 * as "used last time". The actual success handoff happens at /auth/callback
 * (outside this view); recording at activation is the closest observable
 * point here — a failed SSO attempt leaves the previous successful method
 * untouched only if the user never reaches the provider, so this is
 * best-effort. Storage failures are swallowed by useLoginPrefs.
 */
function rememberSsoMethod(method: string) {
  loginPrefs.setLastMethod(method)
  lastUsedMethod.value = method
}

const router = useRouter()
const email = ref('')
const password = ref('')

const { loading, error, mutate: login } = useMutation(async () => {
  const res = await fetch('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email: email.value,
      password: password.value,
      org_slug: singleOrg.value?.slug ?? undefined,
    }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || res.statusText)
  }
  const data = await res.json()
  setAccessToken(data.access_token)
  // FAR-460: sync the must-change-password gate from every manual login
  // response — true forces the full-screen change-password view; false clears
  // any stale flag so a different account is never trapped behind the gate.
  setMustChangePassword(data.must_change_password === true)
  // Successful password login — remember the method for the next visit's
  // "used last time" tag. Best-effort (storage failures are swallowed).
  loginPrefs.setLastMethod('password')
  lastUsedMethod.value = 'password'
  router.push('/')
  return data
})
</script>
