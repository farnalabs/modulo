<template>
  <svg
    :viewBox="`0 0 ${width} ${height}`"
    class="h-full w-full overflow-hidden"
    preserveAspectRatio="none"
    aria-hidden="true"
  >
    <defs>
      <linearGradient :id="gradientId" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" :stop-color="color" stop-opacity="0.2" />
        <stop offset="100%" :stop-color="color" stop-opacity="0.02" />
      </linearGradient>
    </defs>
    <polygon :fill="`url(#${gradientId})`" :points="areaPoints" />
    <polyline
      fill="none"
      :stroke="color"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
      :points="linePoints"
    />
  </svg>
</template>

<script setup lang="ts">
import { computed } from "vue";

// Module-level counter + hash so the gradient id is URL-safe and unique per
// instance. A raw color string like "var(--color-primary)" contains
// parentheses that break the url(#...) reference, and two sparklines on the
// same page must never share an id.
let sparklineUid = 0;

function hashString(value: string): string {
  let hash = 0;
  for (let i = 0; i < value.length; i++) {
    hash = (hash << 5) - hash + value.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash).toString(36);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

defineOptions({ name: "SparklineChart" });

const props = withDefaults(
  defineProps<{
    data: number[];
    color?: string;
    width?: number;
    height?: number;
  }>(),
  {
    color: "currentColor",
    width: 200,
    height: 40,
  },
);

const gradientId = computed(() => {
  const seed = `${props.data.join(",")}|${props.color}`;
  return `sparkline-${++sparklineUid}-${hashString(seed)}`;
});

// Number.isFinite excludes Infinity/-Infinity (isNaN does not), which would
// otherwise leak into max/range and produce NaN, out-of-viewBox geometry.
const normalizedData = computed(() =>
  props.data.filter((v) => typeof v === "number" && Number.isFinite(v)),
);

const chartData = computed(() => {
  if (normalizedData.value.length >= 2) return normalizedData.value;
  if (normalizedData.value.length === 0) return [0, 0];
  return [normalizedData.value[0], normalizedData.value[0]];
});

const max = computed(() => Math.max(...chartData.value, 1));
const min = computed(() => Math.min(...chartData.value));
const range = computed(() => {
  const raw = max.value - min.value;
  return Number.isFinite(raw) && raw > 0 ? raw : 1;
});

const padding = 2;
const stepX = computed(
  () => (props.width - padding * 2) / (chartData.value.length - 1),
);

const linePoints = computed(() => {
  return chartData.value
    .map((v, i) => {
      // Clamp x and y to the viewBox so pathological input can never escape
      // the chart area (previously rendered as a "black mountain" over
      // neighbouring cards via overflow-visible).
      const x = clamp(padding + i * stepX.value, padding, props.width - padding);
      const y = clamp(
        props.height -
          padding -
          ((v - min.value) / range.value) * (props.height - padding * 2),
        padding,
        props.height - padding,
      );
      return `${x},${y}`;
    })
    .join(" ");
});

const areaPoints = computed(() => {
  const pts = chartData.value.map((v, i) => {
    const x = clamp(padding + i * stepX.value, padding, props.width - padding);
    const y = clamp(
      props.height -
        padding -
        ((v - min.value) / range.value) * (props.height - padding * 2),
      padding,
      props.height - padding,
    );
    return `${x},${y}`;
  });
  const firstX = pts[0].split(",")[0];
  const lastX = pts[pts.length - 1].split(",")[0];
  return `${firstX},${props.height - padding} ${pts.join(" ")} ${lastX},${props.height - padding}`;
});
</script>
