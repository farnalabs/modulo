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
        <SidebarLink to="/" icon="LayoutDashboard" label="Dashboard" />
        <SidebarLink to="/library" icon="BookOpen" label="Library" />
        <SidebarLink to="/pipelines" icon="GitBranch" label="Pipelines" />
        <SidebarLink to="/pipelines/copy" icon="Copy" label="Copy Pipeline" />
        <SidebarLink to="/templates" icon="LayoutTemplate" label="Pipeline Templates" />
        <SidebarLink to="/stages" icon="Columns" label="Stages" />
        <SidebarLink to="/evals/editor" icon="CheckSquare" label="Evals" />
        <SidebarLink to="/evals/proposals" icon="Clipboard" label="Eval Proposals" />
        <SidebarLink to="/variants/compare" icon="GitFork" label="Variants" />

        <div class="sidebar-section-header">Settings</div>
        <SidebarLink to="/settings/observability" icon="Eye" label="Observability" />
        <SidebarLink to="/settings/teams" icon="Users" label="Teams" />
        <SidebarLink to="/settings/sso" icon="Shield" label="SSO" />
        <SidebarLink to="/settings/rate-limits" icon="Gauge" label="Rate Limits" />
        <SidebarLink to="/settings/runtime-config" icon="Settings" label="Runtime Config" />
        <SidebarLink to="/settings/license" icon="KeyRound" label="License" />
        <SidebarLink to="/settings/mcp" icon="Cable" label="MCP" />
        <SidebarLink to="/settings/triggers" icon="Zap" label="Triggers" />
        <SidebarLink to="/settings/hitl-review" icon="ShieldQuestion" label="HITL Review" />
        <SidebarLink to="/schemas" icon="Database" label="Schemas" />
        <SidebarLink to="/schemas/editor" icon="Database" label="Schema Editor" />
        <SidebarLink to="/schemas/infer" icon="Database" label="Schema Inference" />

        <div class="sidebar-section-header">Admin</div>
        <SidebarLink to="/admin/users" icon="UserCircle" label="Users" />
        <SidebarLink to="/feedback/inbox" icon="MessageSquare" label="Feedback Inbox" />
        <SidebarLink to="/admin/audit" icon="FileText" label="Audit Log" />
        <SidebarLink to="/admin/connectors" icon="Plug" label="Connectors" />
        <SidebarLink to="/admin/model-backends" icon="Cpu" label="Model Backends" />
        <SidebarLink to="/admin/node-categories" icon="Tag" label="Node Categories" />
        <SidebarLink to="/admin/views" icon="Eye" label="Saved Views" />
        <SidebarLink to="/admin/costs" icon="DollarSign" label="Cost Breakdown" />
        <SidebarLink to="/admin/costs/limits" icon="CreditCard" label="Spend Limits" />
        <SidebarLink to="/admin/costs/controls" icon="SlidersHorizontal" label="Cost Controls" />
        <SidebarLink to="/admin/run-retention" icon="Clock" label="Run Retention" />
        <SidebarLink to="/admin/feature-flags" icon="Flag" label="Feature Flags" />
        <SidebarLink to="/admin/org" icon="Building" label="Org Settings" />
        <SidebarLink to="/admin/plugins" icon="Puzzle" label="Plugins" />
        <SidebarLink to="/admin/api-changelog" icon="History" label="Changelog" />
        <SidebarLink to="/admin/teams/comparison" icon="BarChart" label="Team Comparison" />
        <SidebarLink to="/admin/environments" icon="Container" label="Environments" />
        <SidebarLink to="/admin/notification-delivery" icon="Bell" label="Notification Delivery" />
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
            :title="planStore.isEnterprise && planStore.expiresAt ? 'Expires: ' + new Date(planStore.expiresAt).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' }) : undefined"
          >
            <span
              v-if="planStore.currentTier === 'enterprise'"
              class="badge-plan bg-primary/10 text-primary font-medium"
            >Enterprise</span>
            <span
              v-else
              class="badge-plan"
            >Free</span>
          </router-link>
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
        <SidebarLink to="/" icon="LayoutDashboard" label="Dashboard" @click="mobileOpen = false" />
        <SidebarLink to="/library" icon="BookOpen" label="Library" @click="mobileOpen = false" />
        <SidebarLink to="/pipelines" icon="GitBranch" label="Pipelines" @click="mobileOpen = false" />
        <SidebarLink to="/pipelines/copy" icon="Copy" label="Copy Pipeline" @click="mobileOpen = false" />
        <SidebarLink to="/templates" icon="LayoutTemplate" label="Pipeline Templates" @click="mobileOpen = false" />
        <SidebarLink to="/stages" icon="Columns" label="Stages" @click="mobileOpen = false" />
        <SidebarLink to="/evals/editor" icon="CheckSquare" label="Evals" @click="mobileOpen = false" />
        <SidebarLink to="/evals/proposals" icon="Clipboard" label="Eval Proposals" @click="mobileOpen = false" />
        <SidebarLink to="/variants/compare" icon="GitFork" label="Variants" @click="mobileOpen = false" />

        <div class="sidebar-section-header">Settings</div>
        <SidebarLink to="/settings/observability" icon="Eye" label="Observability" @click="mobileOpen = false" />
        <SidebarLink to="/settings/teams" icon="Users" label="Teams" @click="mobileOpen = false" />
        <SidebarLink to="/settings/sso" icon="Shield" label="SSO" @click="mobileOpen = false" />
        <SidebarLink to="/settings/rate-limits" icon="Gauge" label="Rate Limits" @click="mobileOpen = false" />
        <SidebarLink to="/settings/runtime-config" icon="Settings" label="Runtime Config" @click="mobileOpen = false" />
        <SidebarLink to="/settings/license" icon="KeyRound" label="License" @click="mobileOpen = false" />
        <SidebarLink to="/settings/mcp" icon="Cable" label="MCP" @click="mobileOpen = false" />
        <SidebarLink to="/settings/triggers" icon="Zap" label="Triggers" @click="mobileOpen = false" />
        <SidebarLink to="/settings/hitl-review" icon="ShieldQuestion" label="HITL Review" @click="mobileOpen = false" />
        <SidebarLink to="/schemas" icon="Database" label="Schemas" @click="mobileOpen = false" />
        <SidebarLink to="/schemas/editor" icon="Database" label="Schema Editor" @click="mobileOpen = false" />
        <SidebarLink to="/schemas/infer" icon="Database" label="Schema Inference" @click="mobileOpen = false" />

        <div class="sidebar-section-header">Admin</div>
        <SidebarLink to="/admin/users" icon="UserCircle" label="Users" @click="mobileOpen = false" />
        <SidebarLink to="/feedback/inbox" icon="MessageSquare" label="Feedback Inbox" @click="mobileOpen = false" />
        <SidebarLink to="/admin/audit" icon="FileText" label="Audit Log" @click="mobileOpen = false" />
        <SidebarLink to="/admin/connectors" icon="Plug" label="Connectors" @click="mobileOpen = false" />
        <SidebarLink to="/admin/model-backends" icon="Cpu" label="Model Backends" @click="mobileOpen = false" />
        <SidebarLink to="/admin/node-categories" icon="Tag" label="Node Categories" @click="mobileOpen = false" />
        <SidebarLink to="/admin/views" icon="Eye" label="Saved Views" @click="mobileOpen = false" />
        <SidebarLink to="/admin/costs" icon="DollarSign" label="Cost Breakdown" @click="mobileOpen = false" />
        <SidebarLink to="/admin/costs/limits" icon="CreditCard" label="Spend Limits" @click="mobileOpen = false" />
        <SidebarLink to="/admin/costs/controls" icon="SlidersHorizontal" label="Cost Controls" @click="mobileOpen = false" />
        <SidebarLink to="/admin/run-retention" icon="Clock" label="Run Retention" @click="mobileOpen = false" />
        <SidebarLink to="/admin/feature-flags" icon="Flag" label="Feature Flags" @click="mobileOpen = false" />
        <SidebarLink to="/admin/org" icon="Building" label="Org Settings" @click="mobileOpen = false" />
        <SidebarLink to="/admin/plugins" icon="Puzzle" label="Plugins" @click="mobileOpen = false" />
        <SidebarLink to="/admin/api-changelog" icon="History" label="Changelog" @click="mobileOpen = false" />
        <SidebarLink to="/admin/teams/comparison" icon="BarChart" label="Team Comparison" @click="mobileOpen = false" />
        <SidebarLink to="/admin/environments" icon="Container" label="Environments" @click="mobileOpen = false" />
        <SidebarLink to="/admin/notification-delivery" icon="Bell" label="Notification Delivery" @click="mobileOpen = false" />
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
            :title="planStore.isEnterprise && planStore.expiresAt ? 'Expires: ' + new Date(planStore.expiresAt).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' }) : undefined"
          >
            <span
              v-if="planStore.currentTier === 'enterprise'"
              class="badge-plan bg-primary/10 text-primary font-medium"
            >Enterprise</span>
            <span
              v-else
              class="badge-plan"
            >Free</span>
          </router-link>
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
  </div>
</template>

<script lang="ts">
import { defineComponent, computed, h, resolveComponent } from 'vue'
import { useRoute } from 'vue-router'

const icons: Record<string, string> = {
  LayoutDashboard: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg>',
  BookOpen: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>',
  GitBranch: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>',
  Copy: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  Columns: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="12" y1="3" x2="12" y2="21"/></svg>',
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
  KeyRound: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z"/><circle cx="16.5" cy="7.5" r=".5" fill="currentColor"/></svg>',
  Cable: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2v6"/><path d="M15 2v6"/><path d="M5 8h14"/><path d="M7 8v10a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2V8"/></svg>',
  Tag: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2H2v10l9.29 9.29c.94.94 2.48.94 3.42 0l6.58-6.58c.94-.94.94-2.48 0-3.42L12 2z"/><path d="M7 7h.01"/></svg>',
  Puzzle: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19.439 7.85c-.049.322.059.648.289.878l1.568 1.568c.47.47.706 1.087.706 1.704s-.235 1.233-.706 1.704l-1.611 1.611a.98.98 0 0 1-.837.276c-.47-.07-.802-.48-.968-.925a2.501 2.501 0 1 0-3.214 3.214c.446.166.855.497.925.968a.979.979 0 0 1-.276.837l-1.61 1.611a2.404 2.404 0 0 1-1.705.706 2.404 2.404 0 0 1-1.704-.706l-1.568-1.568a1.026 1.026 0 0 0-.877-.29c-.493.074-.84.504-1.02.968a2.5 2.5 0 1 1-3.237-3.237c.464-.18.894-.527.967-1.02a1.026 1.026 0 0 0-.289-.877l-1.568-1.568A2.404 2.404 0 0 1 1.998 12c0-.617.236-1.234.706-1.704L4.315 8.685a.98.98 0 0 1 .837-.276c.47.07.802.48.968.925a2.501 2.501 0 1 0 3.214-3.214c-.446-.166-.855-.497-.925-.968a.979.979 0 0 1 .276-.837l1.611-1.611a2.404 2.404 0 0 1 1.704-.706c.617 0 1.234.236 1.704.706l1.568 1.568c.23.23.556.338.877.29.493-.074.84-.504 1.02-.969a2.5 2.5 0 1 1 3.237 3.237c-.464.18-.894.527-.967 1.02Z"/></svg>',
  Clock: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
  Cpu: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M20 15h2"/><path d="M9 2v2"/><path d="M9 20v2"/><path d="M2 9h2"/><path d="M20 9h2"/></svg>',
  Zap: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
  Building: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="2" width="16" height="20" rx="2" ry="2"/><path d="M9 22v-4h6v4"/><path d="M8 6h.01"/><path d="M16 6h.01"/><path d="M12 6h.01"/><path d="M12 10h.01"/><path d="M12 14h.01"/><path d="M16 10h.01"/><path d="M16 14h.01"/><path d="M8 10h.01"/><path d="M8 14h.01"/></svg>',
  ShieldQuestion: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9.1 9a3 3 0 0 1 5.82 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  Bell: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>',
  Container: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>',
  LayoutTemplate: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>',
  SlidersHorizontal: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="21" y1="4" x2="14" y2="4"/><line x1="10" y1="4" x2="3" y2="4"/><line x1="21" y1="12" x2="12" y2="12"/><line x1="8" y1="12" x2="3" y2="12"/><line x1="21" y1="20" x2="16" y2="20"/><line x1="12" y1="20" x2="3" y2="20"/><line x1="14" y1="2" x2="14" y2="6"/><line x1="8" y1="10" x2="8" y2="14"/><line x1="16" y1="18" x2="16" y2="22"/></svg>',
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
import { ref, onMounted } from 'vue'
import { getAccessToken, clearAccessToken } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import LogoMark from './LogoMark.vue'

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

onMounted(() => {
  planStore.fetchPlan()
})
</script>
