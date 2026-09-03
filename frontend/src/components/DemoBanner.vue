<template>
  <div
    v-if="isDemo"
    class="flex items-center gap-2 border-l-4 border-l-[hsl(var(--primary))] bg-[hsl(var(--primary)/0.08)] px-6 py-2 text-sm text-foreground"
    role="status"
    data-testid="demo-banner"
  >
    <svg
      class="h-4 w-4 shrink-0 text-[hsl(var(--primary))]"
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" />
      <path d="M12 16v-4M12 8h.01" />
    </svg>
    <span class="font-medium">{{ $t('components.DemoBanner.title') }}</span>
    <span class="text-muted-foreground">{{ $t('components.DemoBanner.subtitle') }}</span>
  </div>
</template>

<script setup lang="ts">
// FAR-535: visible indicator for demo sessions. Reads the persisted demo flag
// once per mount — the flag is cleared together with the session
// (clearAccessToken), so a normal login never shows this banner.
import { isDemoSession } from '../lib/api/auth'

const isDemo = isDemoSession()
</script>
