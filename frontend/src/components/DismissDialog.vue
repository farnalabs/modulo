<template>
  <div class="inline-flex flex-col rounded-lg border bg-background p-4 shadow-sm min-w-[300px] max-w-[400px]">
    <div class="flex-1">
      <p class="text-sm font-medium">{{ $t('components.DismissDialog.dismiss_this_notification') }}</p>
      <p class="mt-1 text-xs text-muted-foreground">
        {{ $t('components.DismissDialog.dismiss_choice_description') }}
      </p>
    </div>
    <div class="mt-3 flex flex-col gap-2">
      <label for="dismissdialog-field-2" class="flex items-center gap-2 text-sm">
        <input id="dismissdialog-field-2" type="radio" v-model="selectedScope" value="self" />
        {{ $t('components.DismissDialog.dismiss_for_me') }}
      </label>
      <label for="dismissdialog-field-1" v-if="canDismissAtScope" class="flex items-center gap-2 text-sm">
        <input id="dismissdialog-field-1" type="radio" v-model="selectedScope" value="scope" />
        {{ scopeLabel }}
      </label>
    </div>
    <div class="mt-4 flex justify-end gap-2">
      <button
        type="button"
        class="rounded-md border px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted transition-colors"
        @click="$emit('cancel')"
      >
        {{ $t('common.cancel') }}
      </button>
      <Button
        type="button"
        variant="default"
        @click="$emit('confirm', selectedScope)"
      >
        {{ $t('components.DismissDialog.dismiss') }}
      </Button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";
import { Button } from '@/components/ui/button'

const { t } = useI18n();

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
    org: t('components.DismissDialog.dismiss_for_all_org_members'),
    admin: t('components.DismissDialog.dismiss_for_all_admins'),
    user: t('components.DismissDialog.dismiss_for_everyone'),
  };
  return labels[props.notification.scope] || t('components.DismissDialog.dismiss_for_everyone');
});
</script>
