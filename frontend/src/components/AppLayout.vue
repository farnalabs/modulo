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

      <nav class="flex-1 space-y-0.5">
        <template v-for="group in navGroups" :key="group.id">
          <SidebarGroup
            v-if="(group.simpleMode || viewMode === 'advanced') && (!group.systemAdminOnly || isSystemAdmin)"
            :id="group.id"
            :label="group.label"
            :collapsed="isGroupCollapsed(group.id, group.defaultCollapsed)"
            @toggle="toggleGroup(group.id)"
          >
            <SidebarLink
              v-for="item in group.items"
              :key="item.to"
              :to="item.to"
              :icon="item.icon"
              :label="item.label"
            />
          </SidebarGroup>
        </template>
      </nav>

      <div class="border-t pt-4 mt-4 space-y-3">
        <div class="flex items-center gap-2">
          <div class="avatar-ring">
            <div
              class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-primary to-teal-600 text-xs font-bold text-primary-foreground"
              :title="userEmail"
            >
              {{ userInitial }}
            </div>
          </div>
          <router-link
            to="/admin/users"
            class="text-sm text-muted-foreground truncate hover:text-foreground transition-colors flex-1 min-w-0"
          >
            {{ userEmail }}
          </router-link>
          <router-link
            to="/settings/license"
            class="shrink-0"
            :title="planStore.isTeam && planStore.expiresAt ? 'Expires: ' + new Date(planStore.expiresAt).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' }) : undefined"
          >
            <span
              v-if="planStore.currentTier === 'team'"
              class="badge-plan bg-primary/10 text-primary font-medium"
            >{{ planStore.getTierLabel(planStore.currentTier) }}</span>
            <span
              v-else
              class="badge-plan"
            >{{ planStore.getTierLabel(planStore.currentTier) }}</span>
          </router-link>
        </div>

        <div class="flex items-center gap-2">
          <button
            @click="setViewMode(viewMode === 'simple' ? 'advanced' : 'simple')"
            class="text-xs text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1.5"
          >
            <svg v-if="viewMode === 'simple'" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            <svg v-else xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
            {{ viewMode === 'simple' ? 'Show all' : 'Show less' }}
          </button>
        </div>

        <div class="flex items-center justify-between">
          <label class="toggle-switch" :class="isLight ? 'light' : 'dark'">
            <span class="track">
              <span class="thumb" />
            </span>
            <span class="flex items-center gap-1">
              <svg v-if="isLight" xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
              <svg v-else xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
              <span>{{ isLight ? 'Light' : 'Dark' }}</span>
            </span>
            <input type="checkbox" class="hidden" @change="toggleTheme" :checked="isLight" />
          </label>
          <button @click="logout" class="text-xs text-muted-foreground hover:text-foreground transition-colors">Sign out</button>
        </div>
      </div>
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
      <nav class="flex-1 space-y-0.5">
        <template v-for="group in navGroups" :key="group.id">
          <SidebarGroup
            v-if="(group.simpleMode || viewMode === 'advanced') && (!group.systemAdminOnly || isSystemAdmin)"
            :id="group.id"
            :label="group.label"
            :collapsed="isGroupCollapsed(group.id, group.defaultCollapsed)"
            @toggle="toggleGroup(group.id)"
          >
            <SidebarLink
              v-for="item in group.items"
              :key="item.to"
              :to="item.to"
              :icon="item.icon"
              :label="item.label"
              @click="mobileOpen = false"
            />
          </SidebarGroup>
        </template>
      </nav>

      <div class="border-t pt-4 mt-4 space-y-3">
        <div class="flex items-center gap-2">
          <div class="avatar-ring">
            <div
              class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-primary to-teal-600 text-xs font-bold text-primary-foreground"
              :title="userEmail"
            >
              {{ userInitial }}
            </div>
          </div>
          <router-link
            to="/admin/users"
            class="text-sm text-muted-foreground truncate hover:text-foreground transition-colors flex-1 min-w-0"
          >
            {{ userEmail }}
          </router-link>
          <router-link
            to="/settings/license"
            class="shrink-0"
            :title="planStore.isTeam && planStore.expiresAt ? 'Expires: ' + new Date(planStore.expiresAt).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' }) : undefined"
          >
            <span
              v-if="planStore.currentTier === 'team'"
              class="badge-plan bg-primary/10 text-primary font-medium"
            >{{ planStore.getTierLabel(planStore.currentTier) }}</span>
            <span
              v-else
              class="badge-plan"
            >{{ planStore.getTierLabel(planStore.currentTier) }}</span>
          </router-link>
        </div>

        <div class="flex items-center gap-2">
          <button
            @click="setViewMode(viewMode === 'simple' ? 'advanced' : 'simple')"
            class="text-xs text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1.5"
          >
            <svg v-if="viewMode === 'simple'" xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            <svg v-else xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
            {{ viewMode === 'simple' ? 'Show all' : 'Show less' }}
          </button>
        </div>

        <div class="flex items-center justify-between">
          <label class="toggle-switch" :class="isLight ? 'light' : 'dark'">
            <span class="track">
              <span class="thumb" />
            </span>
            <span class="flex items-center gap-1">
              <svg v-if="isLight" xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
              <svg v-else xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
              <span>{{ isLight ? 'Light' : 'Dark' }}</span>
            </span>
            <input type="checkbox" class="hidden" @change="toggleTheme" :checked="isLight" />
          </label>
          <button @click="logout" class="text-xs text-muted-foreground hover:text-foreground transition-colors">Sign out</button>
        </div>
      </div>
    </aside>

    <main class="flex-1 overflow-auto bg-background pt-14 md:pt-0">
      <router-view v-slot="{ Component }">
        <transition name="page" mode="out-in">
          <component :is="Component" />
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
import SidebarGroup from './SidebarGroup.vue'
import { navGroups } from '../config/navigation'
import { useSidebar } from '../composables/useSidebar'

const { viewMode, toggleGroup, isGroupCollapsed, setViewMode } = useSidebar()

const planStore = usePlanStore()

const mobileOpen = ref(false)

const isLight = ref(document.documentElement.classList.contains('light'))

function toggleTheme() {
  const root = document.documentElement
  if (root.classList.contains('light')) {
    root.classList.remove('light')
    root.classList.add('dark')
  } else {
    root.classList.remove('dark')
    root.classList.add('light')
  }
  isLight.value = root.classList.contains('light')
}

function logout() {
  clearAccessToken()
  window.location.reload()
}

const userEmail = computed(() => {
  const token = getAccessToken()
  if (!token) return ''
  try {
    const payload = JSON.parse(atob(token.split('.')[1]))
    return payload.sub || ''
  } catch {
    return ''
  }
})

const userInitial = computed(() => {
  const email = userEmail.value
  if (!email) return '?'
  return email.charAt(0).toUpperCase()
})

const isSystemAdmin = computed(() => {
  const token = getAccessToken()
  if (!token) return false
  try {
    const payload = JSON.parse(atob(token.split('.')[1]))
    return payload.is_system_admin === true
  } catch {
    return false
  }
})

onMounted(() => {
  planStore.fetchPlan()
})
</script>
