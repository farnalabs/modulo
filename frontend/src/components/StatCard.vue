<template>
  <component :is="to ? 'router-link' : 'div'" :to="to" data-testid="dashboard-stats-card" class="card card-hover p-4">
    <div class="flex items-center gap-3">
      <div
        class="flex h-9 w-9 items-center justify-center rounded-lg"
        :class="iconBgClass"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <slot name="icon" />
        </svg>
      </div>
      <div class="min-w-0">
        <p class="text-sm font-medium text-muted-foreground">{{ label }}</p>
        <p class="text-2xl font-semibold tabular-nums" :class="valueClass">{{ value }}</p>
      </div>
    </div>
  </component>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  label: string;
  value: number | string;
  color?: "primary" | "success" | "warning" | "destructive" | "muted";
  to?: string;
}>();

const iconBgClass = computed(() => {
  const map: Record<string, string> = {
    primary: "bg-primary/10 text-primary",
    success: "bg-success/10 text-success",
    warning: "bg-warning/10 text-warning",
    destructive: "bg-destructive/10 text-destructive",
    muted: "bg-muted text-muted-foreground",
  };
  return map[props.color ?? "primary"];
});

const valueClass = computed(() => {
  const map: Record<string, string> = {
    success: "text-success",
    warning: "text-warning",
    destructive: "text-destructive",
  };
  return map[props.color ?? "primary"] ?? "";
});
</script>

<style scoped>
.stagger-item:nth-child(1) { animation-delay: 0ms; }
.stagger-item:nth-child(2) { animation-delay: 40ms; }
.stagger-item:nth-child(3) { animation-delay: 80ms; }
.stagger-item:nth-child(4) { animation-delay: 120ms; }
.stagger-item:nth-child(5) { animation-delay: 160ms; }
.stagger-item:nth-child(6) { animation-delay: 200ms; }
</style>
