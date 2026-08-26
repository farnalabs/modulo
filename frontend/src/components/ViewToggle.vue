<template>
  <div v-if="planStore.featureEnabled('saved_views')" data-testid="view-toggle-gate">
    <div class="flex items-center gap-2" data-testid="view-toggle">
      <Select
  aria-label="Form control"
  v-model="selectedViewId"
  @update:model-value="onViewSelect($event as string)"
  :placeholder="$t('components.ViewToggle.select_a_saved_view')"
  data-testid="view-toggle-trigger"
  class="w-[200px]"
  :options="views.map(view => ({ value: view.id, label: view.name }))"
  option-label="label"
  option-value="value"
>
  <template #header
>
{{ $t('components.ViewToggle.saved_views') }}
  </template>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>

      <button type="button"
        v-if="selectedViewId"
        role="switch"
        :aria-checked="isEnabled"
        :class="[
          'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          isEnabled ? 'bg-primary' : 'bg-input',
        ]"
        @click="toggleEnabled"
        data-testid="view-toggle-switch"
      >
        <span
          :class="[
            'pointer-events-none block h-3.5 w-3.5 rounded-full bg-background shadow-lg ring-0 transition-transform',
            isEnabled ? 'translate-x-4' : 'translate-x-0',
          ]"
        />
      </button>

      <Badge
        v-if="selectedViewId"
        :severity="isEnabled ? 'info' : 'secondary'"
        class="text-xs"
        data-testid="view-toggle-badge"
      >
        {{ isEnabled ? "Active" : "Inactive" }}
      </Badge>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api } from "@/lib/api/client";
import { usePlanStore } from "@/stores/planStore";
import Badge from "primevue/badge";
import Select from "primevue/select";

interface SavedView {
  id: string;
  name: string;
}

const emit = defineEmits<{
  (
    e: "view-changed",
    payload: { viewId: string | null; enabled: boolean },
  ): void;
}>();

const planStore = usePlanStore();
const views = ref<SavedView[]>([]);
const selectedViewId = ref<string | null>(null);
const isEnabled = ref(false);

async function fetchViews() {
  try {
    const { data, error } = await api.GET("/api/v1/views");
    if (error) {
      console.warn("ViewToggle: failed to fetch views", error);
      return;
    }
    if (data && Array.isArray(data.items)) {
      views.value = data.items;
    }
  } catch {
    console.warn("ViewToggle: exception fetching views");
  }
}

function onViewSelect(id: string) {
  selectedViewId.value = id;
  emit("view-changed", { viewId: id, enabled: isEnabled.value });
}

function toggleEnabled() {
  isEnabled.value = !isEnabled.value;
  if (selectedViewId.value) {
    emit("view-changed", {
      viewId: selectedViewId.value,
      enabled: isEnabled.value,
    });
  }
}

defineExpose({ selectedViewId, views, isEnabled, fetchViews });

onMounted(() => {
  if (planStore.featureEnabled("saved_views")) {
    fetchViews();
  }
});
</script>
