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

      <div v-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
        {{ error }}
      </div>

      <div v-if="success" class="rounded-lg border border-emerald-500/50 bg-emerald-500/10 p-4 text-sm">
        {{ $t('views.OAuthConsentView.approved_redirecting') }}
      </div>

      <div v-if="!hasToken" class="rounded-xl border bg-card p-6 space-y-4 shadow-sm">
        <p class="text-sm text-muted-foreground">{{ $t('views.OAuthConsentView.login_required') }}</p>
        <Button
          class="w-full"
          variant="default"
          data-testid="oauth-consent-login"
          @click="goToLogin"
        >
          {{ $t('common.sign_in') }}
        </Button>
      </div>

      <div v-else class="rounded-xl border bg-card p-6 space-y-4 shadow-sm">
        <div class="space-y-1">
          <p class="text-sm font-medium">{{ $t('views.OAuthConsentView.requesting_application') }}</p>
          <p class="text-lg font-semibold" data-testid="oauth-consent-client-name">{{ clientName || query.client_id }}</p>
        </div>

        <div class="space-y-2">
          <p class="text-sm font-medium">{{ $t('views.OAuthConsentView.requested_scopes') }}</p>
          <ul class="space-y-1">
            <li
              v-for="scope in query.scope?.split(' ') ?? []"
              :key="scope"
              class="text-sm text-muted-foreground"
              data-testid="oauth-consent-scope"
            >
              {{ scope }}
            </li>
          </ul>
        </div>

        <Button
          :disabled="approving"
          class="w-full border-primary/30 hover:border-primary/60 px-4 py-2.5"
          variant="default"
          data-testid="oauth-consent-approve"
          @click="approve"
        >
          {{ approving ? $t('views.OAuthConsentView.approving') : $t('views.OAuthConsentView.approve') }}
        </Button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button } from '@/components/ui/button'
import { getAccessToken } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'

const route = useRoute()
const router = useRouter()

const query = computed(() => route.query as Record<string, string>)
const hasToken = computed(() => Boolean(getAccessToken()))

const clientName = ref('')
const approving = ref(false)
const success = ref(false)
const error = ref('')

const state = computed(() => query.value.state ?? '')

onMounted(async () => {
  // Display-only client info — never authoritative. The approve endpoint mints
  // the code from the stored consent-state row ONLY (ADR 017 A1b), so a
  // spoofed client name here cannot escalate the granted scope.
  if (!hasToken.value) return
  try {
    const token = getAccessToken()
    const payload = token ? JSON.parse(atob(token.split('.')[1])) : null
    clientName.value = payload?.username ?? ''
  } catch {
    clientName.value = ''
  }
})

function goToLogin() {
  router.push({ name: 'login', query: { redirect: route.fullPath } })
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
      body: JSON.stringify({ state: state.value }),
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
