<template>
  <div class="flex flex-col flex-1 min-h-0">
    <div class="mb-6 flex items-center gap-2.5 pl-1">
      <router-link to="/" class="flex items-center gap-2.5">
        <div
          class="flex items-center justify-center rounded-lg bg-primary/10 p-1.5"
        >
          <LogoMark :size="24" transparent />
        </div>
        <h2 class="text-lg font-bold tracking-tight">{{ $t('components.AppLayout.modulo') }}</h2>
        <Badge v-if="planStore.currentTier" variant="outline" class="text-[10px] px-1.5 py-0 leading-none opacity-70">
          {{ planStore.getTierLabel(planStore.currentTier) }}
        </Badge>
      </router-link>
      <button
        type="button"
        @click="$emit('collapse')"
        class="ml-auto rounded-md p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
        :aria-label="$t('components.AppLayout.collapse_sidebar')"
      >
        <ChevronLeft class="h-4 w-4" aria-hidden="true" />
      </button>
    </div>

    <div class="flex items-center gap-2 pt-2 pb-2 border-b mb-2">
      <router-link
        to="/admin/my-profile"
        class="avatar-ring"
        :aria-label="$t('components.AppLayout.user_profile')"
      >
        <div
          class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xs font-bold text-primary"
          :title="userEmail"
        >
          {{ userInitial }}
        </div>
      </router-link>
      <router-link
        to="/admin/my-profile"
        class="text-sm text-muted-foreground truncate hover:text-foreground transition-colors flex-1 min-w-0"
        :aria-label="$t('components.AppLayout.user_profile')"
      >
        {{ userEmail }}
      </router-link>
    </div>

    <div class="flex items-center justify-between pb-2 mb-1 gap-2">
      <div class="flex items-center gap-1">
        <NotificationBell />
        <button
          type="button"
          @click="$emit('open-command-palette')"
          class="rounded-md p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
          :aria-label="$t('components.AppLayout.search_pages')"
        >
          <Search class="h-[18px] w-[18px]" aria-hidden="true" />
        </button>
      </div>
      <label for="applayout-field-2" class="toggle-switch" :class="isLight ? 'light' : 'dark'">
        <span class="track">
          <span class="thumb" />
        </span>
        <span class="flex items-center gap-1">
          <Sun v-if="isLight" class="h-3 w-3" aria-hidden="true" />
          <Moon v-else class="h-3 w-3" aria-hidden="true" />
        </span>
        <input
          id="applayout-field-2"
          type="checkbox"
          class="sr-only"
          :aria-label="$t('components.AppLayout.toggle_theme')"
          @change="$emit('toggle-theme')"
          :checked="isLight"
        />
      </label>
      <div class="flex-1" />
    </div>

    <SidebarNav
      class="flex-1"
      :is-system-admin="isSystemAdmin"
      :user-role="userRole"
      :user-permissions="userPermissions"
      @navigate="$emit('navigate')"
    />

    <SidebarFooter @logout="$emit('logout')" />
  </div>
</template>

<script setup lang="ts">
import { usePlanStore } from "../stores/planStore";
import Badge from "./ui/badge/Badge.vue";
import LogoMark from "./LogoMark.vue";
import NotificationBell from "./NotificationBell.vue";
import SidebarFooter from "./SidebarFooter.vue";
import SidebarNav from "./SidebarNav.vue";
import { ChevronLeft, Moon, Search, Sun } from "@lucide/vue";

const planStore = usePlanStore();

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
  collapse: [];
  navigate: [];
}>();
</script>
