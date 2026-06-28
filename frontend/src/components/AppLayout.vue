<template>
  <div class="flex min-h-screen">
    <!-- Sidebar -->
    <aside class="flex w-64 border-r bg-background p-4 flex-col">
      <div class="mb-6">
        <h2 class="text-lg font-bold">Modulo</h2>
      </div>

      <nav class="flex-1 space-y-1">
        <SidebarLink to="/" icon="LayoutDashboard" label="Dashboard" />
        <SidebarLink to="/library" icon="BookOpen" label="Library" />
        <SidebarLink to="/pipelines" icon="GitBranch" label="Pipelines" />

        <div class="mt-6 mb-2 text-xs font-medium text-muted-foreground uppercase tracking-wider">Settings</div>
        <SidebarLink to="/settings/observability" icon="Eye" label="Observability" />
        <SidebarLink to="/settings/teams" icon="Users" label="Teams" />
        <SidebarLink to="/settings/sso" icon="Shield" label="SSO" />
        <SidebarLink to="/settings/rate-limits" icon="Gauge" label="Rate Limits" />
        <SidebarLink to="/settings/runtime-config" icon="Settings" label="Runtime Config" />

        <div class="mt-6 mb-2 text-xs font-medium text-muted-foreground uppercase tracking-wider">Admin</div>
        <SidebarLink to="/admin/audit" icon="FileText" label="Audit Log" />
        <SidebarLink to="/admin/feature-flags" icon="Flag" label="Feature Flags" />
        <SidebarLink to="/admin/api-changelog" icon="History" label="Changelog" />
        <SidebarLink to="/admin/teams/comparison" icon="BarChart" label="Team Comparison" />
      </nav>

      <div class="border-t pt-4 mt-4">
        <div class="flex items-center justify-between">
          <span class="text-sm text-muted-foreground truncate">{{ userEmail }}</span>
          <span class="shrink-0 rounded-full bg-green-100 px-2 py-0.5 text-xs text-green-700">Free</span>
        </div>
        <button @click="logout" class="mt-2 text-xs text-muted-foreground hover:text-foreground">Sign out</button>
      </div>
    </aside>

    <!-- Mobile menu button -->
    <button
      @click="mobileOpen = !mobileOpen"
      class="md:hidden fixed top-4 left-4 z-50 rounded-md bg-background border p-2 text-muted-foreground hover:text-foreground"
      aria-label="Toggle navigation"
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <line x1="3" y1="6" x2="21" y2="6"/>
        <line x1="3" y1="12" x2="21" y2="12"/>
        <line x1="3" y1="18" x2="21" y2="18"/>
      </svg>
    </button>

    <!-- Mobile overlay -->
    <div
      v-if="mobileOpen"
      class="md:hidden fixed inset-0 z-40 bg-black/50"
      @click="mobileOpen = false"
    />

    <!-- Mobile sidebar -->
    <aside
      class="md:hidden fixed top-0 left-0 z-50 h-full w-64 border-r bg-background p-4 flex flex-col transition-transform"
      :class="mobileOpen ? 'translate-x-0' : '-translate-x-full'"
    >
      <div class="mb-6 flex items-center justify-between">
        <h2 class="text-lg font-bold">Modulo</h2>
        <button @click="mobileOpen = false" class="text-muted-foreground hover:text-foreground" aria-label="Close navigation">
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>

      <nav class="flex-1 space-y-1">
        <SidebarLink to="/" icon="LayoutDashboard" label="Dashboard" @click="mobileOpen = false" />
        <SidebarLink to="/library" icon="BookOpen" label="Library" @click="mobileOpen = false" />
        <SidebarLink to="/pipelines" icon="GitBranch" label="Pipelines" @click="mobileOpen = false" />

        <div class="mt-6 mb-2 text-xs font-medium text-muted-foreground uppercase tracking-wider">Settings</div>
        <SidebarLink to="/settings/observability" icon="Eye" label="Observability" @click="mobileOpen = false" />
        <SidebarLink to="/settings/teams" icon="Users" label="Teams" @click="mobileOpen = false" />
        <SidebarLink to="/settings/sso" icon="Shield" label="SSO" @click="mobileOpen = false" />
        <SidebarLink to="/settings/rate-limits" icon="Gauge" label="Rate Limits" @click="mobileOpen = false" />
        <SidebarLink to="/settings/runtime-config" icon="Settings" label="Runtime Config" @click="mobileOpen = false" />

        <div class="mt-6 mb-2 text-xs font-medium text-muted-foreground uppercase tracking-wider">Admin</div>
        <SidebarLink to="/admin/audit" icon="FileText" label="Audit Log" @click="mobileOpen = false" />
        <SidebarLink to="/admin/feature-flags" icon="Flag" label="Feature Flags" @click="mobileOpen = false" />
        <SidebarLink to="/admin/api-changelog" icon="History" label="Changelog" @click="mobileOpen = false" />
        <SidebarLink to="/admin/teams/comparison" icon="BarChart" label="Team Comparison" @click="mobileOpen = false" />
      </nav>

      <div class="border-t pt-4 mt-4">
        <div class="flex items-center justify-between">
          <span class="text-sm text-muted-foreground truncate">{{ userEmail }}</span>
          <span class="shrink-0 rounded-full bg-green-100 px-2 py-0.5 text-xs text-green-700">Free</span>
        </div>
        <button @click="logout" class="mt-2 text-xs text-muted-foreground hover:text-foreground">Sign out</button>
      </div>
    </aside>

    <main class="flex-1 overflow-auto">
      <router-view />
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
}

export const SidebarLink = defineComponent({
  name: 'SidebarLink',
  props: { to: { type: String, required: true }, icon: { type: String, required: true }, label: { type: String, required: true } },
  setup(props) {
    const route = useRoute()
    const isActive = computed(() => route.path === props.to || (props.to !== '/' && route.path.startsWith(props.to + '/')))
    const RouterLink = resolveComponent('router-link') as any
    return () => h(RouterLink, { to: props.to, class: `flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors ${isActive.value ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'}` }, [
      h('span', { class: 'h-4 w-4', innerHTML: icons[props.icon] }),
      props.label,
    ])
  },
})
</script>

<script setup lang="ts">
import { ref } from 'vue'
import { getAccessToken, clearAccessToken } from '../lib/api/client'

const mobileOpen = ref(false)

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
</script>
