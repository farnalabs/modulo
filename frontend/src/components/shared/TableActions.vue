<template>
  <div class="flex items-center gap-1">
    <button
      v-for="action in actions"
      :key="action.key"
      type="button"
      @click="action.onClick"
      :disabled="action.disabled"
      class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors hover:bg-accent hover:text-accent-foreground disabled:opacity-50"
      :class="action.danger ? 'text-destructive hover:bg-destructive/10' : 'text-muted-foreground'"
    >
      <component :is="action.icon" v-if="action.icon" class="h-3.5 w-3.5" />
      {{ action.label }}
    </button>
  </div>
</template>

<script setup lang="ts">
import type { Component } from 'vue'

interface Action {
  key: string
  label: string
  icon?: Component
  onClick: () => void
  disabled?: boolean
  danger?: boolean
}

defineProps<{ actions: Action[] }>()
</script>
