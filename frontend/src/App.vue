<template>
  <LoginView v-if="!isAuthenticated" />
  <TooltipProvider v-else-if="isBareRoute" :delay-duration="300">
    <RemyOnlyView />
  </TooltipProvider>
  <AppLayout v-else />
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { getAccessToken, setAccessToken, setRefreshToken, onAuthChange, getInitialAuthState, shouldReRunAutoLogin } from './lib/api/client'
import { getErrorTracker } from './lib/error-tracking'
import { getAutoLoginConfig } from './config/runtime'
import LoginView from './views/LoginView.vue'
import AppLayout from './components/AppLayout.vue'
import RemyOnlyView from './views/RemyOnlyView.vue'
import { TooltipProvider } from './components/ui/tooltip'
import { useWebVitals } from './composables/useWebVitals'

const router = useRouter()
const route = useRoute()

// A stored token means the user is already authenticated — render the app
// immediately. Auto-login (below) only runs when no session exists yet.
const autoLogin = getAutoLoginConfig()
const isAuthenticated = ref(getInitialAuthState(!!getAccessToken()))

// Routes flagged meta.bare (e.g. /remy) render without the AppLayout chrome.
const isBareRoute = computed(() => route.meta.bare === true)

useWebVitals()

// Tracks the previous auth state so the authenticated→cleared transition can
// be detected — that is the trigger for the auto-login recovery path below.
let wasAuthenticated = isAuthenticated.value
let autoLoginRunning = false

// Silent auto-login using the configured credentials. Used both on first mount
// (no stored session) and for recovery when an existing session clears while
// auto-login is configured — without this, an expired stored token (401 →
// refresh failure → clearAccessToken) leaves the user stranded on the login
// screen until a manual reload. Returns whether a session was established.
async function runAutoLogin(): Promise<boolean> {
  if (autoLoginRunning || !autoLogin) return false
  autoLoginRunning = true
  try {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: autoLogin.username, password: autoLogin.password }),
    })
    if (!res.ok) return false
    const data = await res.json()
    setAccessToken(data.access_token)
    if (data.refresh_token) setRefreshToken(data.refresh_token)
    if (data.user) {
      const tracker = getErrorTracker()
      if (tracker) {
        tracker.setUser({
          id: data.user.id,
          email: data.user.email,
          name: data.user.name,
        })
      }
    } else {
      console.warn('[App.vue] Login response has no user field — skipping error tracker setUser')
      // TODO: fetch /me after login to set user info on error tracker
    }
    router.push('/')
    return true
  } catch {
    // Silent — fall back to login screen
    return false
  } finally {
    autoLoginRunning = false
  }
}

onAuthChange((token) => {
  const wasAuthed = wasAuthenticated
  wasAuthenticated = !!token

  // Recovery path: when auto-login is configured and the session transitions
  // from authenticated to cleared (expired stored token → 401 → refresh
  // failure → clearAccessToken), re-run the silent auto-login instead of
  // stranding the user on the login screen. Keep the app rendered while the
  // recovery is in flight so there is no visible login flash; drop to the
  // login screen only if the recovery login also fails.
  if (shouldReRunAutoLogin(wasAuthed, !!token, !!autoLogin)) {
    isAuthenticated.value = true
    runAutoLogin().then((ok) => {
      if (!ok) isAuthenticated.value = false
    })
    return
  }

  isAuthenticated.value = !!token
})

onMounted(() => {
  if (isAuthenticated.value) return
  runAutoLogin()
})
</script>
