<template>
  <div
    v-if="spotlight.active.value"
    class="fixed inset-0 z-[60] bg-black/50"
    @click="handleDismiss"
    data-testid="spotlight-overlay"
  >
    <div
      v-if="elementRect && spotlight.targetElement.value"
      class="absolute rounded-lg border-2 border-primary shadow-[0_0_0_4px_rgba(59,130,246,0.3)] pointer-events-auto"
      :style="cutoutStyle"
      @click.stop
    >
      <div
        v-if="spotlight.message.value"
        class="absolute left-1/2 -translate-x-1/2 mt-2 w-64 rounded-lg bg-background border shadow-lg p-3 text-sm"
        :style="{ top: '100%' }"
      >
        <p class="font-medium text-foreground">{{ spotlight.message.value }}</p>
        <p class="text-xs text-muted-foreground mt-1">Click anywhere to dismiss this guide</p>
      </div>
    </div>

    <div
      v-else
      class="fixed top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 rounded-lg bg-background border shadow-lg p-6 text-center max-w-sm"
    >
      <p class="text-sm text-muted-foreground">
        {{ spotlight.message.value || 'Follow the next step to continue setting up Modulo.' }}
      </p>
      <p class="text-xs text-muted-foreground mt-2">Click anywhere to dismiss this guide</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from 'vue'
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

watch(() => spotlight.target.value, () => {
  if (resizeObserver) resizeObserver.disconnect()
  elementRect.value = null
  if (spotlight.targetElement.value) {
    updateRect()
    resizeObserver = new ResizeObserver(updateRect)
    resizeObserver.observe(document.body)
  }
})

function handleDismiss() {
  spotlight.dismiss()
}

onUnmounted(() => {
  if (resizeObserver) resizeObserver.disconnect()
})
</script>
