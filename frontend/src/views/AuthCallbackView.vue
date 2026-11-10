<template>
  <div class="relative mx-auto flex min-h-screen max-w-md items-center justify-center overflow-x-hidden p-6">
    <div class="relative w-full space-y-6">
      <div class="text-center">
        <div class="mb-4 flex justify-center">
          <div class="flex h-14 w-14 items-center justify-center rounded-xl bg-primary/10 border border-primary/20">
            <LogoMark :size="32" transparent />
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
import LogoMark from '../components/LogoMark.vue'
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
