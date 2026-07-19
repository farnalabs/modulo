<template>
  <router-link
    to="/notifications"
    class="relative inline-flex items-center justify-center rounded-md p-2 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
    :aria-label="$t('components.NotificationBell.notifications')"
  >
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
      <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
    </svg>
    <span
      v-if="unreadCount > 0"
      class="absolute -right-0.5 -top-0.5 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-bold text-destructive-foreground"
    >
      {{ unreadCount > 99 ? '99+' : unreadCount }}
    </span>
  </router-link>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import { fetchUnreadCount } from "../lib/api/notifications";
import { registerHandler } from "../stores/syncRegistry";

const unreadCount = ref(0);

let unsubHandler: (() => void) | null = null;

onMounted(async () => {
  try {
    unreadCount.value = await fetchUnreadCount();
  } catch {
    unreadCount.value = 0;
  }
  unsubHandler = registerHandler("notification", async () => {
    try {
      unreadCount.value = await fetchUnreadCount();
    } catch {
      unreadCount.value = 0;
    }
  });
});

onUnmounted(() => {
  if (unsubHandler) unsubHandler();
});
</script>
