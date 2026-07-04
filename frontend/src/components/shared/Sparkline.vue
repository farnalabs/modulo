<template>
  <div
    v-if="normalizedData.length < 2"
    class="h-full w-full rounded bg-muted/30"
  />
  <svg
    v-else
    :viewBox="`0 0 ${width} ${height}`"
    class="h-full w-full overflow-visible"
    preserveAspectRatio="none"
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
import { computed, defineOptions } from "vue";
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

let gradientCounter = 0;
const gradientId = computed(
  () => `sparkline-${++gradientCounter}`,
);

const normalizedData = computed(() =>
  props.data.filter((v) => typeof v === "number" && !isNaN(v)),
);

const max = computed(() => Math.max(...normalizedData.value, 1));
const min = computed(() => Math.min(...normalizedData.value));
const range = computed(() => max.value - min.value || 1);

const padding = 2;
const stepX = computed(
  () => (props.width - padding * 2) / (normalizedData.value.length - 1),
);

const linePoints = computed(() => {
  return normalizedData.value
    .map((v, i) => {
      const x = padding + i * stepX.value;
      const y =
        props.height -
        padding -
        ((v - min.value) / range.value) * (props.height - padding * 2);
      return `${x},${y}`;
    })
    .join(" ");
});

const areaPoints = computed(() => {
  const pts = normalizedData.value.map((v, i) => {
    const x = padding + i * stepX.value;
    const y =
      props.height -
      padding -
      ((v - min.value) / range.value) * (props.height - padding * 2);
    return `${x},${y}`;
  });
  const firstX = pts[0].split(",")[0];
  const lastX = pts[pts.length - 1].split(",")[0];
  return `${firstX},${props.height - padding} ${pts.join(" ")} ${lastX},${props.height - padding}`;
});
</script>
