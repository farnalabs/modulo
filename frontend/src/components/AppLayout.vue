<template>
  <div class="flex min-h-screen">
    <!-- Sidebar -->
    <aside class="hidden md:flex w-64 border-r bg-background p-4 flex-col">
      <div class="mb-6 flex items-center gap-2.5 pl-1">
        <div class="flex items-center justify-center rounded-lg bg-gradient-to-br from-teal-500/20 to-transparent p-1.5">
          <LogoMark :size="24" transparent />
        </div>
        <h2 class="text-lg font-bold tracking-tight">Modulo</h2>
      </div>

      <SidebarNav :is-system-admin="isSystemAdmin" />

      <SidebarFooter
        :user-email="userEmail"
        :user-initial="userInitial"
        :view-mode="viewMode"
        :is-light="isLight"
        @toggle-theme="toggleTheme"
        @set-view-mode="setViewMode"
        @logout="logout"
      />
    </aside>

    <!-- Mobile header -->
    <header class="md:hidden fixed top-0 left-0 right-0 z-50 flex items-center justify-between border-b bg-background px-4 h-14">
      <button
        @click="mobileOpen = !mobileOpen"
        class="rounded-md p-2 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
        :aria-label="mobileOpen ? 'Close navigation' : 'Open navigation'"
      >
        <svg v-if="!mobileOpen" xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="3" y1="6" x2="21" y2="6"/>
          <line x1="3" y1="12" x2="21" y2="12"/>
          <line x1="3" y1="18" x2="21" y2="18"/>
        </svg>
        <svg v-else xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="18" y1="6" x2="6" y2="18"/>
          <line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
      <div class="flex items-center gap-2.5">
        <div class="flex items-center justify-center rounded-lg bg-gradient-to-br from-teal-500/20 to-transparent p-1.5">
          <LogoMark :size="24" transparent />
        </div>
        <h2 class="text-lg font-bold tracking-tight">Modulo</h2>
      </div>
    </header>

    <!-- Mobile overlay -->
    <div
      v-if="mobileOpen"
      class="md:hidden fixed inset-0 z-30 bg-black/50"
      @click="mobileOpen = false"
    />

    <!-- Mobile sidebar -->
    <aside
      class="md:hidden fixed top-14 left-0 z-40 h-[calc(100vh-3.5rem)] w-64 border-r bg-background p-4 flex flex-col transition-transform"
      :class="mobileOpen ? 'translate-x-0' : '-translate-x-full'"
    >
      <SidebarNav :is-system-admin="isSystemAdmin" @navigate="mobileOpen = false" />

      <SidebarFooter
        :user-email="userEmail"
        :user-initial="userInitial"
        :view-mode="viewMode"
        :is-light="isLight"
        @toggle-theme="toggleTheme"
        @set-view-mode="setViewMode"
        @logout="logout"
      />
    </aside>

    <main class="flex-1 overflow-auto bg-background pt-14 md:pt-0">
      <router-view v-slot="{ Component, route }">
        <transition name="page" mode="out-in">
          <component :is="Component" :key="route.fullPath" />
        </transition>
      </router-view>
    </main>

    <RemyPanel />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { getAccessToken, clearAccessToken } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import LogoMark from './LogoMark.vue'
import RemyPanel from './remy/RemyPanel.vue'
import SidebarFooter from './SidebarFooter.vue'
import SidebarNav from './SidebarNav.vue'
import { useSidebar } from '../composables/useSidebar'

const { viewMode, setViewMode } = useSidebar()

const planStore = usePlanStore()

const mobileOpen = ref(false)

const isLight = ref(document.documentElement.classList.contains('light'))

function toggleTheme() {
  const root = document.documentElement
  root.classList.toggle('light')
  root.classList.toggle('dark')
  isLight.value = root.classList.contains('light')
}

function logout() {
  clearAccessToken()
  window.location.reload()
}

const jwtPayload = computed(() => {
  const token = getAccessToken()
  if (!token) return null
  try {
    return JSON.parse(atob(token.split('.')[1]))
  } catch {
    return null
  }
})

const userEmail = computed(() => jwtPayload.value?.sub || '')

const userInitial = computed(() => {
  const email = userEmail.value
  if (!email) return '?'
  return email.charAt(0).toUpperCase()
})

const isSystemAdmin = computed(() => jwtPayload.value?.is_system_admin === true)

onMounted(() => {
  planStore.fetchPlan().catch((err) => {
    console.error('Failed to fetch plan:', err)
  })
})
</script>
