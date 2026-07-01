<template>
  <FeatureGate feature-name="saved_views" data-testid="view-toggle-gate">
    <div class="flex items-center gap-2" data-testid="view-toggle">
      <Select v-model="selectedViewId" @update:model-value="onViewSelect($event as string)">
        <SelectTrigger class="w-[200px]" data-testid="view-toggle-trigger">
          <SelectValue placeholder="Select a saved view..." />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectLabel>Saved Views</SelectLabel>
            <SelectItem
              v-for="view in views"
              :key="view.id"
              :value="view.id"
              data-testid="view-toggle-item"
            >
              {{ view.name }}
            </SelectItem>
          </SelectGroup>
          <div
            v-if="views.length === 0"
            class="px-2 py-4 text-center text-sm text-muted-foreground"
            data-testid="view-toggle-empty"
          >
            No saved views
          </div>
        </SelectContent>
      </Select>

      <button
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
        :variant="isEnabled ? 'default' : 'secondary'"
        class="text-xs"
        data-testid="view-toggle-badge"
      >
        {{ isEnabled ? "Active" : "Inactive" }}
      </Badge>
    </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api } from "@/lib/api/client";
import { usePlanStore } from "@/stores/planStore";
import FeatureGate from "./FeatureGate.vue";
import Badge from "./ui/badge/Badge.vue";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "./ui/select";

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
    const { data, error } = await (api as any).GET("/api/v1/views");
    if (error) {
      return;
    }
    if (data && Array.isArray(data.views)) {
      views.value = data.views;
    }
  } catch (_e) {
    // Silently handle
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
