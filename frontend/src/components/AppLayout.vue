<template>
  <div class="flex min-h-screen">
    <!-- Sidebar -->
    <aside class="hidden md:flex w-64 border-r bg-background p-4 flex-col shadow-lg shadow-black/10">
      <div class="flex items-center gap-2.5 pl-1 pb-4 border-b border-border">
          <div class="flex items-center justify-center rounded-lg bg-primary/10 p-1.5">
          <LogoMark :size="24" transparent />
        </div>
        <h2 class="text-lg font-bold tracking-tight">Modulo</h2>
      </div>

      <nav class="flex-1 space-y-0.5">
        <template v-for="item in navItems" :key="item.label">
          <div v-if="item.type === 'section'" class="sidebar-section-header">{{ item.label }}</div>
          <SidebarLink v-else :to="item.to!" :icon="item.icon!" :label="item.label!" />
        </template>
      </nav>

      <div class="border-t pt-4 mt-4 space-y-3">
        <div class="flex items-center gap-2">
          <div class="avatar-ring">
            <div
              class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground"
              :title="userEmail"
            >
              {{ userInitial }}
            </div>
          </div>
          <router-link
            to="/admin/my-profile"
            class="text-sm text-muted-foreground truncate hover:text-foreground transition-colors flex-1 min-w-0"
          >
            {{ userEmail }}
          </router-link>
          <span class="badge-plan shrink-0">Free</span>
        </div>

        <div class="flex items-center justify-between border-t border-border pt-3 pb-2">
          <label class="toggle-switch" :class="isLight ? 'light' : 'dark'" title="Ctrl+Shift+L to toggle">
            <span class="track">
              <span class="thumb" />
            </span>
            <span class="flex items-center gap-1">
              <svg v-if="isLight" xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
              <svg v-else xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
              <span class="text-xs font-medium">{{ isLight ? 'Light' : 'Dark' }}</span>
            </span>
            <input type="checkbox" class="hidden" @change="toggleTheme" :checked="isLight" />
          </label>
          <button @click="logout" class="text-xs text-muted-foreground hover:text-foreground transition-colors">Sign out</button>
        </div>
      </div>
    </aside>

    <!-- Content column: header + menu + pages -->
    <div class="flex flex-1 flex-col min-h-screen overflow-hidden">
      <!-- Mobile header (hidden on desktop) -->
      <div class="md:hidden bg-background border-b border-border px-4 py-3 flex items-center gap-3 shrink-0">
        <button
          @click="mobileOpen = !mobileOpen"
          class="rounded-md bg-background border border-border p-2 text-muted-foreground hover:text-foreground shrink-0"
          aria-label="Toggle navigation"
        >
          <svg v-if="!mobileOpen" xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="3" y1="6" x2="21" y2="6"/>
            <line x1="3" y1="12" x2="21" y2="12"/>
            <line x1="3" y1="18" x2="21" y2="18"/>
          </svg>
          <svg v-else xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
        <div class="flex items-center gap-2">
          <div class="flex items-center justify-center rounded-lg bg-primary/10 p-1.5">
            <LogoMark :size="20" transparent />
          </div>
          <span class="text-sm font-bold tracking-tight">Modulo</span>
        </div>
      </div>

      <!-- Mobile menu panel -->
      <div
        v-if="mobileOpen"
        class="md:hidden bg-background border-b border-border overflow-y-auto shrink-0"
        style="max-height: calc(100vh - 56px);"
      >
        <nav class="px-4 py-3 space-y-0.5">
          <template v-for="item in navItems" :key="item.label">
            <div v-if="item.type === 'section'" class="text-xs font-semibold text-muted-foreground uppercase tracking-wider px-3 py-2 mt-2">{{ item.label }}</div>
            <router-link
              v-else
              :to="item.to"
              class="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm text-foreground hover:bg-accent transition-colors"
              @click="mobileOpen = false"
            >
              <span class="h-4 w-4 shrink-0 text-muted-foreground" v-html="icons[item.icon]" />
              <span>{{ item.label }}</span>
            </router-link>
          </template>
        </nav>
        <div class="border-t border-border px-4 py-3 flex items-center justify-between">
          <div class="flex items-center gap-2">
            <div class="flex h-7 w-7 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground">
              {{ userInitial }}
            </div>
            <span class="text-sm text-muted-foreground truncate max-w-[120px]">{{ userEmail }}</span>
          </div>
          <button @click="logout" class="text-xs text-muted-foreground hover:text-foreground">Sign out</button>
        </div>
      </div>

      <main class="flex-1 overflow-auto">
        <router-view v-slot="{ Component }">
          <transition name="page" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
</template>

<script lang="ts">
import { defineComponent, computed, h, resolveComponent } from 'vue'
import { useRoute } from 'vue-router'

const icons: Record<string, string> = {
  LayoutDashboard: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg>',
  BookOpen: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>',
  GitBranch: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>',
  Users: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
  Shield: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  Gauge: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 15.5v-3"/><path d="M9 12l3-3 3 3"/><circle cx="12" cy="12" r="10"/></svg>',
  Settings: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  Eye: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  FileText: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>',
  Flag: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>',
  BarChart: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/></svg>',
  History: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  UserCircle: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="5"/><path d="M3 21v-2a7 7 0 0 1 7-7h4a7 7 0 0 1 7 7v2"/></svg>',
  CheckSquare: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>',
  GitFork: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><circle cx="18" cy="6" r="3"/><path d="M18 9v1a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V9"/><path d="M12 12v3"/></svg>',
  Database: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
  MessageSquare: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
}

export const SidebarLink = defineComponent({
  name: 'SidebarLink',
  props: { to: { type: String, required: true }, icon: { type: String, required: true }, label: { type: String, required: true } },
  setup(props) {
    const route = useRoute()
    const isActive = computed(() => route.path === props.to || (props.to !== '/' && route.path.startsWith(props.to + '/')))
    const RouterLink = resolveComponent('router-link') as any
    return () => h(RouterLink, { to: props.to, class: `sidebar-link ${isActive.value ? 'active' : ''}` }, [
      h('span', { class: 'h-4 w-4 shrink-0', innerHTML: icons[props.icon] }),
      h('span', { class: 'truncate' }, props.label),
    ])
  },
})
</script>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { getAccessToken, clearAccessToken } from '../lib/api/client'
import LogoMark from './LogoMark.vue'

interface NavSection {
  type: 'section'
  label: string
}

interface NavLink {
  type: 'link'
  to: string
  icon: string
  label: string
}

type NavItem = NavSection | NavLink

const navItems: NavItem[] = [
  { type: 'link', to: '/', icon: 'LayoutDashboard', label: 'Dashboard' },
  { type: 'link', to: '/library', icon: 'BookOpen', label: 'Library' },
  { type: 'link', to: '/pipelines', icon: 'GitBranch', label: 'Pipelines' },
  { type: 'link', to: '/evals/editor', icon: 'CheckSquare', label: 'Evals' },
  { type: 'link', to: '/variants/compare', icon: 'GitFork', label: 'Variants' },
  { type: 'section', label: 'Settings' },
  { type: 'link', to: '/settings/observability', icon: 'Eye', label: 'Observability' },
  { type: 'link', to: '/settings/teams', icon: 'Users', label: 'Teams' },
  { type: 'link', to: '/settings/sso', icon: 'Shield', label: 'SSO' },
  { type: 'link', to: '/settings/rate-limits', icon: 'Gauge', label: 'Rate Limits' },
  { type: 'link', to: '/settings/runtime-config', icon: 'Settings', label: 'Runtime Config' },
  { type: 'link', to: '/schemas/infer', icon: 'Database', label: 'Schema Inference' },
  { type: 'section', label: 'Admin' },
  { type: 'link', to: '/admin/my-profile', icon: 'UserCircle', label: 'My Profile' },
  { type: 'link', to: '/admin/users', icon: 'Users', label: 'Users' },
  { type: 'link', to: '/feedback/inbox', icon: 'MessageSquare', label: 'Feedback Inbox' },
  { type: 'link', to: '/admin/audit', icon: 'FileText', label: 'Audit Log' },
  { type: 'link', to: '/admin/feature-flags', icon: 'Flag', label: 'Feature Flags' },
  { type: 'link', to: '/admin/api-changelog', icon: 'History', label: 'Changelog' },
  { type: 'link', to: '/admin/teams/comparison', icon: 'BarChart', label: 'Team Comparison' },
]

const mobileOpen = ref(false)

const isLight = ref(document.documentElement.classList.contains('light'))

function toggleTheme() {
  const root = document.documentElement
  if (root.classList.contains('light')) {
    root.classList.remove('light')
  } else {
    root.classList.add('light')
  }
  isLight.value = root.classList.contains('light')
}

function handleKeydown(e: KeyboardEvent) {
  if (e.ctrlKey && e.shiftKey && e.key === 'L') {
    e.preventDefault()
    toggleTheme()
  }
}

onMounted(() => {
  document.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
})

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
</script>
