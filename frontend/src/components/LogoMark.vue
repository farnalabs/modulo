<script setup lang="ts">
defineProps<{
  size?: number;
  transparent?: boolean;
}>();
</script>

<template>
  <svg
    :width="size ?? 40"
    :height="size ?? 40"
    viewBox="0 0 100 100"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    role="img"
    :aria-label="$t('components.LogoMark.modulo_logo')"
  >
    <rect
      v-if="!transparent"
      width="100"
      height="100"
      rx="12"
      fill="currentColor"
      class="text-ink-900"
    />
    <g
      stroke="#00FFD1"
      stroke-width="7"
      fill="none"
      stroke-linejoin="round"
      stroke-linecap="round"
    >
      <line x1="30" y1="84.64" x2="70" y2="15.36" />
      <polygon points="36,28 31,36.66 21,36.66 16,28 21,19.34 31,19.34" />
      <polygon points="84,72 79,80.66 69,80.66 64,72 69,63.34 79,63.34" />
    </g>
  </svg>
</template>
