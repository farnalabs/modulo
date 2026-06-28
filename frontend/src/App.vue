<template>
  <LoginView v-if="!isAuthenticated" />
  <AppLayout v-else />
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { getAccessToken, setAccessToken, onAuthChange } from './lib/api/client'
import LoginView from './views/LoginView.vue'
import AppLayout from './components/AppLayout.vue'

const router = useRouter()
const isAuthenticated = ref(!!getAccessToken())

onAuthChange((token) => {
  isAuthenticated.value = !!token
})

onMounted(async () => {
  if (isAuthenticated.value) return

  const username = import.meta.env.VITE_AUTO_LOGIN_USERNAME as string | undefined
  const password = import.meta.env.VITE_AUTO_LOGIN_PASSWORD as string | undefined
  if (!username || !password) return

  try {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: username, password }),
    })
    if (!res.ok) return
    const data = await res.json()
    setAccessToken(data.access_token)
    router.push('/')
  } catch {
    // Silent — fall back to login screen
  }
})
</script>
