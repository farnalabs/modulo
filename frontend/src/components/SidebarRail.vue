<template>
  <div class="flex flex-col items-center gap-1 h-full w-full">
    <button
      type="button"
      @click="$emit('expand')"
      class="flex h-10 w-10 items-center justify-center rounded-md p-2 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
      :aria-label="$t('components.AppLayout.expand_sidebar')"
    >
      <ChevronRight class="h-4 w-4" aria-hidden="true" />
    </button>

    <router-link
      to="/"
      class="flex h-10 w-10 items-center justify-center rounded-md hover:bg-muted transition-colors"
      :aria-label="$t('components.AppLayout.modulo')"
    >
      <LogoMark :size="24" transparent />
    </router-link>

    <router-link
      to="/admin/my-profile"
      class="avatar-ring"
      :aria-label="$t('components.AppLayout.user_profile')"
    >
      <div
        class="flex h-7 w-7 items-center justify-center rounded-full bg-primary/10 text-xs font-bold text-primary"
        :title="userEmail"
      >
        {{ userInitial }}
      </div>
    </router-link>

    <NotificationBell />

    <button
      type="button"
      @click="$emit('open-command-palette')"
      class="flex h-10 w-10 items-center justify-center rounded-md p-2 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
      :aria-label="$t('components.AppLayout.search_pages')"
    >
      <Search class="h-[18px] w-[18px]" aria-hidden="true" />
    </button>

    <button
      type="button"
      @click="$emit('toggle-theme')"
      class="flex h-10 w-10 items-center justify-center rounded-full p-2 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
      :aria-label="$t('components.AppLayout.toggle_theme')"
    >
      <Sun v-if="isLight" class="h-[18px] w-[18px]" aria-hidden="true" />
      <Moon v-else class="h-[18px] w-[18px]" aria-hidden="true" />
    </button>

    <div class="my-1 w-full border-t" aria-hidden="true" />

    <SidebarNav
      :collapsed="true"
      class="flex-1 min-h-0 w-full"
      :is-system-admin="isSystemAdmin"
      :user-role="userRole"
      :user-permissions="userPermissions"
      @navigate="$emit('navigate')"
    />

    <div class="my-1 w-full border-t" aria-hidden="true" />

    <SidebarFooter :collapsed="true" @logout="$emit('logout')" />
  </div>
</template>

<script setup lang="ts">
import LogoMark from "./LogoMark.vue";
import NotificationBell from "./NotificationBell.vue";
import SidebarFooter from "./SidebarFooter.vue";
import SidebarNav from "./SidebarNav.vue";
import { ChevronRight, Moon, Search, Sun } from "@lucide/vue";

defineProps<{
  isSystemAdmin: boolean;
  userRole?: string | null;
  userPermissions?: string[];
  userEmail: string;
  userInitial: string;
  isLight: boolean;
}>();

defineEmits<{
  "toggle-theme": [];
  logout: [];
  "open-command-palette": [];
  expand: [];
  navigate: [];
}>();</script>
