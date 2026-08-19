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
          aria-hidden="true"
        >
          <slot name="icon" />
        </svg>
      </div>
      <div class="min-w-0 flex-1">
        <p class="text-sm font-medium text-muted-foreground">{{ label }}</p>
        <div class="flex items-baseline gap-2">
          <p class="text-2xl font-semibold tabular-nums" :class="valueClass">{{ value }}</p>
          <span
            v-if="deltaPct != null"
            class="inline-flex items-center gap-0.5 text-xs font-medium"
            :class="deltaClass"
          >
            <span aria-hidden="true">{{ deltaArrow }}</span>{{ deltaAbsText }} ({{ deltaPctText }})
          </span>
          <span
            v-else-if="deltaAbs != null && noBaselineLabel"
            class="inline-flex items-center gap-0.5 text-xs font-medium"
            :class="deltaAbsClass"
            data-testid="stat-no-baseline"
            role="img"
            :aria-label="noBaselineLabel"
            :title="noBaselineLabel"
          >
            <span aria-hidden="true">{{ deltaAbsArrow }}</span>{{ deltaAbsText }}
          </span>
        </div>
      </div>
    </div>
  </component>
</template>

<script setup lang="ts">
import { computed } from "vue";

export interface StatDelta {
  current?: number | null;
  previous?: number | null;
  delta_pct?: number | null;
}

const props = defineProps<{
  label: string;
  value: number | string;
  color?: "primary" | "success" | "warning" | "destructive" | "muted";
  to?: string;
  delta?: StatDelta | null;
  noBaselineLabel?: string;
  inverted?: boolean;
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

const deltaPct = computed(() => {
  const d = props.delta?.delta_pct;
  return typeof d === "number" && Number.isFinite(d) ? d : null;
});

const deltaAbs = computed(() => {
  const { current, previous } = props.delta ?? {};
  if (typeof current !== "number" || typeof previous !== "number") return null;
  if (!Number.isFinite(current) || !Number.isFinite(previous)) return null;
  return current - previous;
});

const deltaAbsText = computed(() => {
  const d = deltaAbs.value;
  if (d == null) return "";
  if (d === 0) return "0";
  const sign = d > 0 ? "+" : "-";
  const abs = Math.abs(d);
  const formatted = Number.isInteger(abs) ? String(abs) : abs.toFixed(1);
  return `${sign}${formatted}`;
});

function arrowFor(delta: number, inverted: boolean): string {
  if (delta > 0) return inverted ? '▼' : '▲'
  if (delta < 0) return inverted ? '▲' : '▼'
  return '→'
}

function classFor(delta: number, inverted: boolean): string {
  if (delta > 0) return inverted ? 'text-destructive' : 'text-success'
  if (delta < 0) return inverted ? 'text-success' : 'text-destructive'
  return 'text-muted-foreground'
}

const deltaArrow = computed(() => {
  const d = deltaPct.value;
  if (d == null) return "";
  return arrowFor(d, props.inverted ?? false);
});

const deltaAbsArrow = computed(() => {
  const d = deltaAbs.value;
  if (d == null) return "";
  return arrowFor(d, props.inverted ?? false);
});

const deltaClass = computed(() => {
  const d = deltaPct.value;
  if (d == null) return "";
  return classFor(d, props.inverted ?? false);
});

const deltaAbsClass = computed(() => {
  const d = deltaAbs.value;
  if (d == null) return "";
  return classFor(d, props.inverted ?? false);
});

const deltaPctText = computed(() => {
  const d = deltaPct.value;
  if (d == null) return "";
  return `${Math.abs(d).toFixed(1)}%`;
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
