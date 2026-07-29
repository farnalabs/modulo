<template>
  <router-link
    to="/notifications"
    class="relative inline-flex items-center justify-center rounded-md p-2 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
    :aria-label="$t('components.NotificationBell.notifications')"
  >
    <Bell class="h-[18px] w-[18px]" aria-hidden="true" />
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
import { Bell } from "@lucide/vue";

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

