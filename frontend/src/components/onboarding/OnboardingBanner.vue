<template>
  <div v-if="store.isActive" class="onboarding-banner">
    <div
      class="flex items-center gap-3 px-6 py-4 cursor-pointer border-b border-l-4 border-l-primary bg-gradient-to-r from-primary/5 to-primary/10 bg-card hover:bg-accent/50 transition-colors"
      role="button"
      tabindex="0"
      :aria-expanded="expanded"
      @click="expanded = !expanded"
      @keydown.enter.prevent="expanded = !expanded"
      @keydown.space.prevent="expanded = !expanded"
      data-testid="onboarding-banner-trigger"
    >
      <div class="relative h-10 w-10 shrink-0">
        <svg class="h-10 w-10 -rotate-90" viewBox="0 0 32 32">
          <circle cx="16" cy="16" r="14" fill="none" stroke="hsl(var(--border))" stroke-width="3" />
          <circle
            cx="16" cy="16" r="14"
            fill="none"
            stroke="hsl(var(--primary))"
            stroke-width="3"
            stroke-linecap="round"
            :stroke-dasharray="circumference"
            :stroke-dashoffset="dashOffset"
          />
        </svg>
        <span class="absolute inset-0 flex items-center justify-center text-xs font-semibold text-primary">
          {{ Math.round(store.progressPct) }}%
        </span>
      </div>
      <div class="min-w-0 flex-1">
        <p class="text-base font-semibold">{{ $t('components.onboarding.OnboardingBanner.set_up_modulo') }}</p>
        <p class="text-xs text-muted-foreground">
          {{ store.completedCount }} of {{ store.totalActions }} actions completed
          <template v-if="store.currentAction"> — Next: {{ store.currentAction.title }}</template>
        </p>
      </div>
      <svg
        class="h-4 w-4 shrink-0 text-muted-foreground transition-transform"
        :class="{ 'rotate-180': expanded }"
        xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      >
        <polyline points="6 9 12 15 18 9" />
      </svg>
    </div>

    <div v-if="expanded" class="border-b bg-card px-4 py-3 space-y-1" data-testid="onboarding-banner-checklist">
        <p class="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">{{ $t('components.onboarding.OnboardingBanner.recommended_actions') }}</p>
        <div
          v-for="action in store.actions"
          :key="action.id"
          class="flex items-center gap-3 rounded-lg px-3 py-2 transition-colors"
          :class="action.completed || action.skipped ? 'opacity-50' : 'hover:bg-accent cursor-pointer'"
          role="button"
          tabindex="0"
          @click="handleActionClick(action)"
          @keydown.enter.prevent="handleActionClick(action)"
          @keydown.space.prevent="handleActionClick(action)"
          :data-testid="`onboarding-action-${action.id}`"
        >
          <div class="flex h-6 w-6 shrink-0 items-center justify-center">
            <svg v-if="action.completed" class="h-5 w-5 text-success" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
              <circle cx="12" cy="12" r="10" />
              <path d="m9 12 2 2 4-4" />
            </svg>
            <svg v-else-if="action.skipped" class="h-5 w-5 text-muted-foreground" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="10" />
              <line x1="8" y1="12" x2="16" y2="12" />
            </svg>
            <div v-else class="h-5 w-5 rounded-full border-2 border-muted-foreground/40 flex items-center justify-center">
              <span class="text-[10px] font-medium text-muted-foreground">{{ action.order }}</span>
            </div>
          </div>

          <div class="min-w-0 flex-1">
            <p class="text-sm font-medium">{{ action.title }}</p>
            <p class="text-xs text-muted-foreground">{{ action.description }}</p>
          </div>

          <button type="button"
            v-if="!action.completed && !action.skipped"
            class="shrink-0 text-xs text-muted-foreground hover:text-foreground underline"
            @click.stop="handleSkip(action)"
            data-testid="onboarding-skip-action"
          >
            Skip
          </button>
        </div>

        <div v-if="store.error" class="text-xs text-destructive px-3 py-1 rounded bg-destructive/10">
          {{ store.error }}
        </div>
        <div class="flex items-center justify-between pt-3 mt-1 border-t">
          <button type="button"
            v-if="store.actions.some(a => !a.completed && !a.skipped)"
            class="text-xs text-muted-foreground hover:text-foreground underline"
            @click="handleDismiss"
            data-testid="onboarding-dismiss"
          >
            Dismiss
          </button>
          <button type="button"
            class="text-xs text-primary hover:underline"
            @click="handleSeed"
            data-testid="onboarding-seed-examples"
          >
            Seed example primitives
          </button>
        </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useOnboardingStore, type OnboardingAction } from '../../composables/useOnboarding'

const store = useOnboardingStore()
const router = useRouter()
const expanded = ref(false)

const circumference = 2 * Math.PI * 14
const dashOffset = computed(() => circumference - (store.progressPct / 100) * circumference)

function handleActionClick(action: OnboardingAction) {
  if (action.completed || action.skipped) return
  if (action.route) {
    router.push(action.route)
  }
  expanded.value = false
}

function handleSkip(action: OnboardingAction) {
  store.skipAction(action.id)
}

function handleDismiss() {
  store.dismiss()
  expanded.value = false
}

async function handleSeed() {
  await store.seedExamples()
}

onMounted(() => {
  if (!store.ready) {
    store.fetchStatus()
  }
})
</script>

<style scoped>
.onboarding-banner {
  /* In normal flow so it reserves vertical space instead of overlaying
     the content below it (WCAG 2.5.8: interactive targets underneath must
     not be partially obscured). Takes no space when inactive (v-if on root). */
  position: relative;
}
</style>
