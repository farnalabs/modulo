<template>
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
        to="/admin/my-profile"
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
        type="button"
        :aria-pressed="viewMode === 'advanced'"
        :aria-label="viewMode === 'simple' ? 'Show all navigation groups' : 'Show fewer navigation groups'"
        @click="$emit('setViewMode', viewMode === 'simple' ? 'advanced' : 'simple')"
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
        <input type="checkbox" class="sr-only" @change="$emit('toggleTheme')" :checked="isLight" />
      </label>
      <button type="button" @click="$emit('logout')" class="text-xs text-muted-foreground hover:text-foreground transition-colors">Sign out</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { usePlanStore } from '../stores/planStore'

defineProps<{
  userEmail: string
  userInitial: string
  viewMode: 'simple' | 'advanced'
  isLight: boolean
}>()

defineEmits<{
  toggleTheme: []
  setViewMode: [mode: 'simple' | 'advanced']
  logout: []
}>()

const planStore = usePlanStore()
</script>
