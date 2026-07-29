<template>
  <div class="mt-auto pt-8">
    <button
      type="button"
      @click="$emit('logout')"
      class="w-full text-sm text-muted-foreground hover:text-foreground transition-colors text-center justify-center flex items-center gap-2"
    >
      <LogOut class="h-[14px] w-[14px]" aria-hidden="true" />
      {{ $t('common.sign_out') }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { LogOut } from "@lucide/vue";

defineEmits<{
  logout: [];
}>();
</script>

