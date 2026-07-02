<template>
  <div class="inline-flex items-center rounded-lg border bg-background p-4 shadow-sm min-w-[300px] max-w-[400px]">
    <div class="flex-1">
      <p class="text-sm font-medium">{{ $t('components.DismissDialog.dismiss_this_notification') }}</p>
      <p class="mt-1 text-xs text-muted-foreground">
        Choose whether to dismiss for yourself or for everyone who can see this notification.
      </p>
    </div>
    <div class="mt-3 flex flex-col gap-2">
      <label class="flex items-center gap-2 text-sm">
        <input type="radio" v-model="selectedScope" value="self" />
        Dismiss for me
      </label>
      <label v-if="canDismissAtScope" class="flex items-center gap-2 text-sm">
        <input type="radio" v-model="selectedScope" value="scope" />
        {{ scopeLabel }}
      </label>
    </div>
    <div class="mt-4 flex justify-end gap-2">
      <button
        type="button"
        class="rounded-md border px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted transition-colors"
        @click="$emit('cancel')"
      >
        Cancel
      </button>
      <button
        type="button"
        class="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 transition-colors"
        @click="$emit('confirm', selectedScope)"
      >
        Dismiss
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from "vue";

const props = defineProps<{
  notification: { scope: string; dismiss_strategy: string; dismissible_at_scope: boolean };
}>();

defineEmits<{
  cancel: [];
  confirm: [scope: "self" | "scope"];
}>();

const selectedScope = ref<"self" | "scope">("self");

const canDismissAtScope = computed(() => {
  return props.notification.dismissible_at_scope;
});

const scopeLabel = computed(() => {
  const labels: Record<string, string> = {
    org: "Dismiss for all org members",
    admin: "Dismiss for all admins",
    user: "Dismiss for everyone",
  };
  return labels[props.notification.scope] || "Dismiss for everyone";
});
</script>
