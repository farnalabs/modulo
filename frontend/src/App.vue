<template>
  <LoginView v-if="!isAuthenticated" />
  <AppLayout v-else />
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { getAccessToken, setAccessToken, setRefreshToken, onAuthChange } from './lib/api/client'
import { getErrorTracker } from './lib/error-tracking'
import { getAutoLoginConfig } from './config/runtime'
import LoginView from './views/LoginView.vue'
import AppLayout from './components/AppLayout.vue'
import { useWebVitals } from './composables/useWebVitals'

const router = useRouter()

// When auto-login is configured, don't trust localStorage tokens at
// startup — wait for the auto-login API call to complete first.
const autoLogin = getAutoLoginConfig()
const hasAutoLogin = !!autoLogin
const isAuthenticated = ref(hasAutoLogin ? false : !!getAccessToken())

useWebVitals()

onAuthChange((token) => {
  isAuthenticated.value = !!token
})

onMounted(async () => {
  if (isAuthenticated.value) return

  if (!autoLogin) return

  try {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: autoLogin.username, password: autoLogin.password }),
    })
    if (!res.ok) return
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
  } catch {
    // Silent — fall back to login screen
  }
})
</script>
