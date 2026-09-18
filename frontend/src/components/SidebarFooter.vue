<template>
  <div :class="collapsed ? 'mt-auto pt-2' : 'mt-auto pt-8'">
    <button
      type="button"
      @click="$emit('logout')"
      class="text-muted-foreground hover:text-foreground transition-colors"
      :class="collapsed
        ? 'flex h-10 w-10 items-center justify-center rounded-md hover:bg-muted'
        : 'w-full text-sm text-center justify-center flex items-center gap-2'"
      :title="collapsed ? $t('common.sign_out') : undefined"
      :aria-label="collapsed ? $t('common.sign_out') : undefined"
    >
      <LogOut :class="collapsed ? 'h-[18px] w-[18px]' : 'h-[14px] w-[14px]'" aria-hidden="true" />
      <span v-if="!collapsed">{{ $t('common.sign_out') }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { LogOut } from "@lucide/vue";

defineProps<{
  collapsed?: boolean;
}>();

defineEmits<{
  logout: [];
}>();
</script>
