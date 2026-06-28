<template>
  <div class="flex min-h-screen">
    <aside class="hidden md:flex w-64 border-r bg-background p-4 flex-col">
      <div class="mb-6 flex items-center gap-2.5 pl-1">
        <h2 class="text-lg font-bold tracking-tight">Modulo</h2>
      </div>
      <nav class="flex-1 space-y-0.5">
        <router-link to="/" class="sidebar-link">Dashboard</router-link>
        <router-link to="/library" class="sidebar-link">Library</router-link>
        <router-link to="/admin/users" class="sidebar-link">Users</router-link>
      </nav>
    </aside>

    <div class="md:hidden fixed top-0 left-0 right-0 z-50 bg-background border-b border-border px-4 py-3 flex items-center justify-between">
      <div class="flex items-center gap-2">
        <span class="text-sm font-bold">Modulo</span>
      </div>
      <button @click="mobileOpen = !mobileOpen" class="rounded-md bg-background border border-border p-2" aria-label="Toggle navigation">
        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="3" y1="6" x2="21" y2="6"></line>
          <line x1="3" y1="12" x2="21" y2="12"></line>
          <line x1="3" y1="18" x2="21" y2="18"></line>
        </svg>
      </button>
    </div>

    <div v-if="mobileOpen" class="md:hidden bg-background border-b border-border">
      <nav class="px-4 py-3 space-y-1">
        <router-link to="/" class="flex items-center gap-3 px-3 py-2 rounded-lg text-sm hover:bg-accent" @click="mobileOpen = false">Dashboard</router-link>
        <router-link to="/library" class="flex items-center gap-3 px-3 py-2 rounded-lg text-sm hover:bg-accent" @click="mobileOpen = false">Library</router-link>
        <router-link to="/admin/users" class="flex items-center gap-3 px-3 py-2 rounded-lg text-sm hover:bg-accent" @click="mobileOpen = false">Users</router-link>
      </nav>
    </div>

    <main class="flex-1 overflow-auto md:pt-0 pt-14">
      <router-view v-slot="{ Component }">
        <transition name="page" mode="out-in">
          <component :is="Component"></component>
        </transition>
      </router-view>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const mobileOpen = ref(false)
</script>
