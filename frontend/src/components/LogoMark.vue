<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  size?: number;
  transparent?: boolean;
}>();

/**
 * The Farna Control mark is NOT square: its brand viewBox is 240 x 183.72
 * (aspect 1.3063:1). `size` therefore sets the HEIGHT and the width is derived
 * from the mark's own aspect ratio, so the art is never squashed or letterboxed.
 */
const MARK_WIDTH = 240;
const MARK_HEIGHT = 183.72;
const MARK_ASPECT = MARK_WIDTH / MARK_HEIGHT;

const markHeight = computed(() => props.size ?? 40);
const markWidth = computed(
  () => Math.round((props.size ?? 40) * MARK_ASPECT * 100) / 100,
);
</script>

<template>
  <svg
    :width="markWidth"
    :height="markHeight"
    viewBox="0 0 240 183.72"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    role="img"
    aria-label="Farna Control logo"
    :class="transparent ? undefined : 'text-white'"
  >
    <rect
      v-if="!transparent"
      width="240"
      height="183.72"
      rx="28.8"
      fill="currentColor"
      class="text-ink-900"
    />
    <!-- Brand mark geometry, copied verbatim from
         farna-control-logo-svgs/farna-control-mark-dark-bg.svg and drawn in
         currentColor so it stays monochrome. -->
    <g
      transform="translate(-55.14 -88.02) scale(3.24)"
      fill="none"
      stroke="currentColor"
      stroke-width="6"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <polygon
        points="52,46.14 44,60 28,60 20,46.14 28,32.28 44,32.28"
        fill="currentColor"
      />
      <polygon points="88,66.93 80,80.79 64,80.79 56,66.93 64,53.07 80,53.07" />
      <line x1="52" y1="46.14" x2="79.71" y2="30.14" />
    </g>
  </svg>
</template>
