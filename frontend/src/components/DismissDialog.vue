<template>
  <Teleport to="body">
    <div
      v-if="modelValue"
      class="fixed inset-0 z-50"
      @click.self="$emit('update:modelValue', false)"
      @keydown.escape="$emit('update:modelValue', false)"
    >
      <!-- Backdrop -->
      <div class="fixed inset-0 bg-black/20" aria-hidden="true" />
      <!-- Popover panel -->
      <div
        ref="panelRef"
        class="fixed z-50 flex flex-col rounded-lg border bg-background p-4 shadow-lg min-w-[300px] max-w-[400px]"
        role="dialog"
        :aria-label="$t('components.DismissDialog.dismiss_this_notification')"
        :style="panelStyle"
      >
        <div class="flex-1">
          <p class="text-sm font-medium">{{ $t('components.DismissDialog.dismiss_this_notification') }}</p>
          <p class="mt-1 text-xs text-muted-foreground">
            {{ $t('components.DismissDialog.dismiss_choice_description') }}
          </p>
        </div>
        <div class="mt-3 flex flex-col gap-2">
          <label for="dismissdialog-radio-self" class="flex items-center gap-2 text-sm">
            <input id="dismissdialog-radio-self" type="radio" v-model="selectedScope" value="self" />
            {{ $t('components.DismissDialog.dismiss_for_me') }}
          </label>
          <label v-if="canDismissAtScope" for="dismissdialog-radio-scope" class="flex items-center gap-2 text-sm">
            <input id="dismissdialog-radio-scope" type="radio" v-model="selectedScope" value="scope" />
            {{ scopeLabel }}
          </label>
        </div>
        <div class="mt-4 flex justify-end gap-2">
          <button
            type="button"
            class="rounded-md border px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-muted transition-colors"
            @click="$emit('update:modelValue', false)"
          >
            {{ $t('common.cancel') }}
          </button>
          <Button type="button" @click="onConfirm">
            {{ $t('components.DismissDialog.dismiss') }}
          </Button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from "vue";
import { useI18n } from "vue-i18n";
import Button from 'primevue/button'

const { t } = useI18n();

const props = defineProps<{
  notification: { scope: string; dismiss_strategy: string; dismissible_at_scope: boolean };
  modelValue: boolean;
  triggerRef?: HTMLElement | null;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: boolean];
  confirm: [scope: "self" | "scope"];
}>();

const selectedScope = ref<"self" | "scope">("self");
const panelRef = ref<HTMLElement | null>(null);
const panelStyle = ref<Record<string, string>>({});

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

function positionPanel() {
  if (!props.triggerRef) {
    // Fallback: center of viewport
    panelStyle.value = { left: '50%', top: '50%', transform: 'translate(-50%, -50%)' };
    return;
  }
  const rect = props.triggerRef.getBoundingClientRect();
  const panelWidth = 340;
  const panelHeight = 260;
  const gap = 8;

  // Try below the trigger first
  let top = rect.bottom + gap;
  let left = rect.left;

  // If it would overflow bottom, try above
  if (top + panelHeight > window.innerHeight) {
    top = rect.top - panelHeight - gap;
  }

  // If it would overflow right, shift left
  if (left + panelWidth > window.innerWidth) {
    left = window.innerWidth - panelWidth - 16;
  }

  // If it would overflow left, clamp
  left = Math.max(16, left);

  // If still overflow top, center vertically
  if (top < 16) {
    top = Math.max(16, (window.innerHeight - panelHeight) / 2);
  }

  panelStyle.value = { left: `${left}px`, top: `${top}px` };
}

watch(() => props.modelValue, async (open) => {
  if (open) {
    selectedScope.value = "self";
    await nextTick();
    positionPanel();
    // Focus the first radio for keyboard accessibility
    const firstRadio = panelRef.value?.querySelector('input[type="radio"]') as HTMLElement | null;
    firstRadio?.focus();
  }
});

function onConfirm() {
  emit("confirm", selectedScope.value);
  emit("update:modelValue", false);
}
</script>
