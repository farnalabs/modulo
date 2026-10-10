<template>
  <div class="relative mx-auto flex min-h-screen max-w-md items-center justify-center p-6">
    <div
      class="pointer-events-none fixed inset-0 -z-10"
      style="background-image: radial-gradient(circle at 1px 1px, var(--dot-color) 1px, transparent 0); background-size: 32px 32px;"
    />

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
        <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.OAuthConsentView.authorize_access') }}</h1>
        <p class="mt-1 text-muted-foreground">{{ $t('views.OAuthConsentView.consent_description') }}</p>
      </div>

      <div
        v-if="error"
        role="alert"
        class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive"
        data-testid="oauth-consent-error"
      >
        {{ error }}
      </div>

      <div
        v-if="contextError"
        role="alert"
        class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive"
        data-testid="oauth-consent-context-error"
      >
        {{ contextError }}
      </div>

      <div
        v-if="success"
        role="status"
        aria-live="polite"
        class="rounded-lg border border-emerald-500/50 bg-emerald-500/10 p-4 text-sm"
        data-testid="oauth-consent-success"
      >
        {{ $t('views.OAuthConsentView.approved_redirecting') }}
      </div>

      <div
        v-if="declined"
        role="status"
        aria-live="polite"
        class="rounded-xl border bg-card p-6 space-y-2 shadow-sm"
        data-testid="oauth-consent-declined"
      >
        <p class="text-lg font-semibold">{{ $t('views.OAuthConsentView.declined_title') }}</p>
        <p class="text-sm text-muted-foreground">{{ $t('views.OAuthConsentView.declined_description') }}</p>
      </div>

      <div v-if="!hasToken && !declined" class="rounded-xl border bg-card p-6 space-y-4 shadow-sm">
        <p class="text-sm text-muted-foreground">{{ $t('views.OAuthConsentView.login_required') }}</p>
        <Button class="w-full" data-testid="oauth-consent-login" @click="goToLogin">
          {{ $t('common.sign_in') }}
        </Button>
      </div>

      <div
        v-else-if="hasToken && !declined && loadingContext"
        role="status"
        aria-live="polite"
        class="rounded-xl border bg-card p-6 shadow-sm"
        data-testid="oauth-consent-loading"
      >
        <p class="text-sm text-muted-foreground">{{ $t('views.OAuthConsentView.loading_request') }}</p>
      </div>

      <div v-else-if="hasToken && !declined && context" class="rounded-xl border bg-card p-6 space-y-4 shadow-sm">
        <div class="space-y-1">
          <p class="text-sm font-medium">{{ $t('views.OAuthConsentView.requesting_application') }}</p>
          <p class="text-lg font-semibold" data-testid="oauth-consent-client-name">{{ context.client_name }}</p>
        </div>

        <p
          v-if="context.team"
          class="rounded-lg border bg-muted/50 p-3 text-sm text-muted-foreground"
          data-testid="oauth-consent-team-line"
        >
          {{ $t('views.OAuthConsentView.team_limited', { name: context.team.name }) }}
        </p>

        <fieldset class="space-y-2">
          <legend class="text-sm font-medium">{{ $t('views.OAuthConsentView.requested_scopes') }}</legend>
          <label
            v-for="scope in context.scopes"
            :key="scope"
            class="flex items-start gap-3 rounded-lg border p-3"
            :data-testid="`oauth-consent-scope-${scope}`"
          >
            <input
              v-model="grantedScopes"
              type="checkbox"
              class="mt-0.5 h-4 w-4"
              :value="scope"
              :data-testid="`oauth-consent-scope-toggle-${scope}`"
            />
            <span class="block">
              <span class="block text-sm">{{ scopeLabel(scope) }}</span>
              <span class="block text-xs text-muted-foreground">{{ scope }}</span>
            </span>
          </label>
        </fieldset>

        <p
          v-if="grantedScopes.length === 0"
          role="status"
          aria-live="polite"
          class="text-sm text-muted-foreground"
          data-testid="oauth-consent-no-scopes-hint"
        >
          {{ $t('views.OAuthConsentView.no_scopes_selected') }}
        </p>

        <div class="space-y-2">
          <Button
            :disabled="approving || grantedScopes.length === 0"
            class="w-full border-primary/30 hover:border-primary/60 px-4 py-2.5"
            data-testid="oauth-consent-approve"
            @click="approve"
          >
            {{ approving ? $t('views.OAuthConsentView.approving') : $t('views.OAuthConsentView.approve') }}
          </Button>
          <Button
            variant="outlined"
            class="w-full px-4 py-2.5"
            data-testid="oauth-consent-decline"
            @click="decline"
          >
            {{ $t('views.OAuthConsentView.decline') }}
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import { getAccessToken } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'

interface ConsentContextTeam {
  id: string
  name: string
}

interface ConsentContext {
  client_name: string
  scopes: string[]
  team: ConsentContextTeam | null
}

// FAR-1476 slice 3: reuse the registration scope picker's i18n keys (the
// SettingsMcpView scope_*_desc family) rather than inventing a second
// labelling scheme. Unknown keys degrade to the raw scope key.
const SCOPE_LABEL_KEYS: Record<string, string> = {
  'trigger:run': 'views.SettingsMcpView.scope_trigger_run_desc',
  'hitl:review': 'views.SettingsMcpView.scope_hitl_review_desc',
  'library:browse': 'views.SettingsMcpView.scope_library_browse_desc',
}

const route = useRoute()
const router = useRouter()
const { t } = useI18n()

const query = computed(() => route.query as Record<string, string>)
const hasToken = computed(() => Boolean(getAccessToken()))
const state = computed(() => query.value.state ?? '')

const context = ref<ConsentContext | null>(null)
const grantedScopes = ref<string[]>([])
const loadingContext = ref(false)
const approving = ref(false)
const success = ref(false)
const declined = ref(false)
const error = ref('')
const contextError = ref('')

function scopeLabel(scope: string): string {
  const key = SCOPE_LABEL_KEYS[scope]
  return key ? t(key) : scope
}

onMounted(async () => {
  if (!state.value) {
    contextError.value = t('views.OAuthConsentView.context_load_error')
    return
  }
  // Display-only context — never authoritative. The approve endpoint mints the
  // code from the stored consent-state row ONLY (ADR 047 A1b), and its
  // granted_scopes can only NARROW that stored set, so a spoofed context here
  // cannot escalate the granted scope. The endpoint is anonymous (the browser
  // may not be signed in yet), so it is fetched without a Bearer header.
  loadingContext.value = true
  try {
    const res = await fetch(`/api/v1/mcp/oauth/consent/context?state=${encodeURIComponent(state.value)}`)
    if (!res.ok) {
      contextError.value = t('views.OAuthConsentView.context_load_error')
      return
    }
    const data = (await res.json()) as ConsentContext
    context.value = data
    // Every requested scope starts granted; per-scope deny is a toggle off.
    grantedScopes.value = [...data.scopes]
  } catch {
    contextError.value = t('views.OAuthConsentView.context_load_error')
  } finally {
    loadingContext.value = false
  }
})

function goToLogin() {
  router.push({ name: 'login', query: { redirect: route.fullPath } })
}

function decline() {
  // The context endpoint deliberately does not expose redirect_uri, so the
  // browser cannot hand the OAuth error back to the client safely (a
  // query-supplied redirect would be an open redirect). Render a clear
  // "you declined" state instead; the pending state simply expires (~15 min).
  declined.value = true
  error.value = ''
}

async function approve() {
  if (!state.value) return
  approving.value = true
  error.value = ''
  try {
    const res = await fetch('/api/v1/mcp/oauth/consent/approve', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${getAccessToken()}`,
      },
      // The still-granted canonical keys; the backend fails closed on any
      // key outside the stored set and mints the code from the intersection.
      body: JSON.stringify({ state: state.value, granted_scopes: [...grantedScopes.value] }),
    })
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      throw new Error(body.detail || res.statusText)
    }
    const data = await res.json()
    success.value = true
    window.location.href = data.redirect_url
  } catch (e) {
    error.value = formatApiError(e)
  } finally {
    approving.value = false
  }
}
</script>
