<template>
  <!-- The backdrop is a real button so pointer dismissal has a keyboard
       equivalent. The coachmark card is a sibling, NOT a child, so its
       focusable "Next" control stays exposed to assistive tech rather than
       sitting inside a backdrop subtree. -->
  <div v-if="spotlight.active.value" class="fixed inset-0 z-[60]" data-testid="spotlight-overlay">
    <button
      type="button"
      class="absolute inset-0 bg-black/50"
      :aria-label="$t('common.close')"
      data-testid="spotlight-backdrop"
      @click="handleDismiss"
    />
    <div
      v-if="elementRect && spotlight.targetElement.value"
      class="absolute rounded-lg border-2 border-primary shadow-[0_0_0_4px_rgba(59,130,246,0.3)] pointer-events-auto"
      :style="cutoutStyle"
      @click.stop
      @keydown.enter.stop
      @keydown.space.prevent.stop
    >
        <div
         v-if="spotlight.message.value"
         class="absolute left-1/2 -translate-x-1/2 mt-2 w-64 rounded-lg bg-background border shadow-lg p-3 text-sm"
         :style="{ top: '100%' }"
         aria-live="polite"
       >
         <p class="font-medium text-foreground">{{ spotlight.message.value }}</p>
         <div class="mt-2 flex items-center justify-between gap-2">
           <p class="text-xs text-muted-foreground">{{ $t('components.onboarding.SpotlightOverlay.dismiss_hint') }}</p>
           <button
             v-if="spotlight.hasNext.value"
             type="button"
             class="shrink-0 rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent"
             @click="spotlight.next"
           >
             {{ $t('components.onboarding.SpotlightOverlay.next') }}
           </button>
         </div>
       </div>
    </div>

    <div
      v-else
      class="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 rounded-lg bg-background border shadow-lg p-6 text-center max-w-sm"
      aria-live="polite"
    >
      <p class="text-sm text-muted-foreground">
        {{ spotlight.message.value || 'Follow the next step to continue setting up Modulo.' }}
      </p>
      <p class="text-xs text-muted-foreground mt-2">{{ $t('components.onboarding.SpotlightOverlay.dismiss_hint') }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { spotlight } from '../../composables/useSpotlight'

const elementRect = ref<DOMRect | null>(null)

const cutoutStyle = computed(() => {
  if (!elementRect.value) return { display: 'none' }
  const r = elementRect.value
  return {
    left: `${r.left}px`,
    top: `${r.top}px`,
    width: `${r.width}px`,
    height: `${r.height}px`,
  }
})

let resizeObserver: ResizeObserver | null = null

function updateRect() {
  const el = spotlight.targetElement.value
  if (el) {
    elementRect.value = el.getBoundingClientRect()
  } else {
    elementRect.value = null
  }
}

function removePositionListeners() {
  window.removeEventListener('scroll', updateRect, true)
  window.removeEventListener('resize', updateRect)
}

function addPositionListeners() {
  window.addEventListener('scroll', updateRect, true)
  window.addEventListener('resize', updateRect)
}

watch(() => spotlight.target.value, () => {
  if (resizeObserver) resizeObserver.disconnect()
  removePositionListeners()
  elementRect.value = null
  if (spotlight.targetElement.value) {
    updateRect()
    resizeObserver = new ResizeObserver(updateRect)
    resizeObserver.observe(document.body)
    addPositionListeners()
  }
}, { immediate: true })

function handleDismiss() {
  spotlight.dismiss()
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && spotlight.active.value) {
    handleDismiss()
  }
}

onMounted(() => {
  document.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
  if (resizeObserver) resizeObserver.disconnect()
  removePositionListeners()
})
</script>
