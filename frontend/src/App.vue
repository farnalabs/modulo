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
// True while a silent auto-login recovery is in flight. Guards the auth-change
// handler so a concurrent 401 clearing the token again during recovery cannot
// flash LoginView — the in-flight recovery owns the final auth state.
let recovering = false

// Silent auto-login using the configured credentials. Used both on first mount
// (no stored session) and for recovery when an existing session clears while
// auto-login is configured — without this, an expired stored token (401 →
// refresh failure → clearAccessToken) leaves the user stranded on the login
// screen until a manual reload. Returns whether a session was established.
// `navigateHome` redirects to the app root on success — true for first-mount
// auto-login, false for mid-session recovery (which must not yank the user off
// a deep link such as /runs/123 back to the dashboard).
async function runAutoLogin(navigateHome = false): Promise<boolean> {
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
    if (navigateHome) router.push('/')
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
    recovering = true
    isAuthenticated.value = true
    runAutoLogin(false).then((ok) => {
      recovering = false
      if (!ok) isAuthenticated.value = false
    })
    return
  }

  // A concurrent 401 during in-flight recovery clears the token again; that
  // must not flip the app back to LoginView while recovery is still running.
  // The recovery branch above owns the final state when it resolves.
  if (recovering) return

  isAuthenticated.value = !!token
})

onMounted(() => {
  if (isAuthenticated.value) return
  runAutoLogin(true)
})
</script>
