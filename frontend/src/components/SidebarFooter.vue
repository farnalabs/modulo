<template>
  <div class="mt-auto pt-8">
    <button
      type="button"
      @click="$emit('logout')"
      class="w-full text-sm text-muted-foreground hover:text-foreground transition-colors text-center justify-center flex items-center gap-2"
    >
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
        <polyline points="16 17 21 12 16 7" />
        <line x1="21" y1="12" x2="9" y2="12" />
      </svg>
      {{ $t('common.sign_out') }}
    </button>
  </div>
</template>

<script setup lang="ts">
defineEmits<{
  logout: [];
}>();
</script>
