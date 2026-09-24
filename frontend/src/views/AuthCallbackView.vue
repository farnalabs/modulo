<template>
  <div class="relative mx-auto flex min-h-screen max-w-md items-center justify-center overflow-x-hidden p-6">
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
        <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.AuthCallbackView.signing_in') }}</h1>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { setAccessToken } from '../lib/api/client'
import { syncFromMe } from '../lib/mustChangePassword'

const router = useRouter()

onMounted(async () => {
  const params = new URLSearchParams(window.location.hash.slice(1))
  const accessToken = params.get('access_token')
  if (!accessToken) {
    router.replace('/login')
    return
  }
  // FAR-1197: the SSO redirect no longer carries a refresh_token — the refresh
  // token rides the httpOnly `modulo_refresh` cookie the backend sets on the
  // same response.
  setAccessToken(accessToken)
  // Strip the tokens from the URL so they are not left in the address bar /
  // browser history after the handoff is consumed.
  history.replaceState(null, '', window.location.pathname + window.location.search)
  // Sync the gate before navigating: stored state may belong to a previously
  // logged-in account, and an SSO-only account could never satisfy a
  // current-password form. Fails open on error.
  await syncFromMe()
  router.replace('/')
})
</script>
