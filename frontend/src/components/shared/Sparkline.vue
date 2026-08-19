<template>
  <div
    ref="containerRef"
    class="relative"
    data-testid="sparkline"
    @pointermove="onPointerMove"
    @pointerleave="onPointerLeave"
  >
    <svg
      :viewBox="`0 0 ${adjustedWidth} ${height}`"
      class="h-full w-full overflow-hidden"
      preserveAspectRatio="none"
      role="img"
      :aria-label="accessibleLabel"
    >
      <defs>
        <linearGradient :id="gradientId" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" :stop-color="color" stop-opacity="0.2" />
          <stop offset="100%" :stop-color="color" stop-opacity="0.02" />
        </linearGradient>
      </defs>
      <template v-if="hasData">
        <polygon :fill="`url(#${gradientId})`" :points="areaPoints" />
        <polyline
          fill="none"
          :stroke="color"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
          :points="linePoints"
        />
        <!-- Y-axis labels -->
        <template v-if="showYAxis">
          <text
            v-for="(val, ti) in yTickValues"
            :key="'ytick-' + ti"
            x="0"
            :y="yForAxis(val)"
            font-size="9"
            :fill="color"
            fill-opacity="0.6"
            dominant-baseline="middle"
            class="sparkline-y-label"
          >{{ formatValue(val) }}</text>
        </template>
        <!-- X-axis ticks -->
        <template v-if="showXTicks">
          <line
            v-for="ti in xTickIndices"
            :key="'xtick-' + ti"
            :x1="xForAxis(ti)"
            :y1="height - padding"
            :x2="xForAxis(ti)"
            :y2="height - padding + 2"
            :stroke="color"
            stroke-opacity="0.3"
            stroke-width="1"
          />
        </template>
      </template>
      <!-- Muted placeholder when there are fewer than two real data points — a
           fabricated line would read as a misleading zero/flat trend. -->
      <template v-else>
        <line
          :x1="adjustedPadding"
          :y1="height / 2"
          :x2="adjustedWidth - padding"
          :y2="height / 2"
          :stroke="color"
          stroke-opacity="0.25"
          stroke-dasharray="3 3"
        />
        <text
          :x="adjustedWidth / 2"
          :y="height / 2"
          text-anchor="middle"
          dominant-baseline="middle"
          font-size="10"
          :fill="color"
          fill-opacity="0.5"
          class="sparkline-no-data"
        >
          {{ t('components.Sparkline.no_data') }}
        </text>
      </template>
    </svg>

    <!-- Hover tooltip: the accessible label on the svg already carries the full
         series, so the tooltip is supplementary and hidden from AT to avoid
         double-announcement. -->
    <div
      v-if="tooltipVisible"
      class="pointer-events-none absolute z-10 rounded-md border bg-background px-2 py-1 text-xs shadow"
      :style="tooltipStyle"
      data-testid="sparkline-tooltip"
      aria-hidden="true"
    >
      {{ tooltipText }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";

const { t } = useI18n();

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
    // Optional x-axis labels (e.g. dates) shown in the hover tooltip.
    labels?: string[];
    // Optional unit (e.g. "runs", "%", "$") appended to values.
    unit?: string;
    // Show y-axis labels (min/max values on left side).
    showYAxis?: boolean;
    // Show x-axis tick marks at data points.
    showXTicks?: boolean;
    // Number of y-axis ticks (default 3).
    tickCount?: number;
  }>(),
  {
    color: "currentColor",
    width: 200,
    height: 60,
    labels: () => [],
    unit: "",
    showYAxis: false,
    showXTicks: false,
    tickCount: 3,
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

// A sparkline needs at least two points to draw a meaningful line. Fewer than
// that renders the "no data" placeholder instead of a fabricated line.
const hasData = computed(() => normalizedData.value.length >= 2);

const chartData = computed(() => normalizedData.value);

const max = computed(() => Math.max(...chartData.value));
const min = computed(() => Math.min(...chartData.value));
const range = computed(() => max.value - min.value);

const padding = 2;
const stepX = computed(
  () => (props.width - padding * 2) / (chartData.value.length - 1),
);

function xFor(i: number): number {
  return clamp(adjustedPadding.value + i * stepX.value, adjustedPadding.value, adjustedWidth.value - padding);
}

function yFor(v: number): number {
  const span = props.height - padding * 2;
  // A flat series (range 0) must not pin the line to the edge of the chart —
  // (v - min) / range would be 0 for every point, drawing a line hugging the
  // border that reads as broken. Centre it vertically so it reads as a steady
  // value.
  const ratio = range.value > 0 ? (v - min.value) / range.value : 0.5;
  return clamp(
    props.height - padding - ratio * span,
    padding,
    props.height - padding,
  );
}

const linePoints = computed(() =>
  chartData.value.map((v, i) => `${xFor(i)},${yFor(v)}`).join(" "),
);

const areaPoints = computed(() => {
  const pts = chartData.value.map((v, i) => `${xFor(i)},${yFor(v)}`);
  const firstX = xFor(0);
  const lastX = xFor(chartData.value.length - 1);
  return `${firstX},${props.height - padding} ${pts.join(" ")} ${lastX},${props.height - padding}`;
});

// --- Axis labels / ticks ----------------------------------------------------
const yAxisLabelWidth = computed(() => (props.showYAxis ? 40 : 0));

const adjustedWidth = computed(() => props.width + yAxisLabelWidth.value);

const adjustedPadding = computed(() => padding + yAxisLabelWidth.value);

function xForAxis(i: number): number {
  return clamp(adjustedPadding.value + i * stepX.value, adjustedPadding.value, adjustedWidth.value - padding);
}

function yForAxis(v: number): number {
  const span = props.height - padding * 2;
  const ratio = range.value > 0 ? (v - min.value) / range.value : 0.5;
  return clamp(
    props.height - padding - ratio * span,
    padding,
    props.height - padding,
  );
}

const yTickValues = computed(() => {
  if (!props.showYAxis || !hasData.value) return [];
  const n = Math.max(2, props.tickCount);
  const step = range.value > 0 ? range.value / (n - 1) : 0;
  return Array.from({ length: n }, (_, i) => min.value + step * i);
});

const xTickIndices = computed(() => {
  if (!props.showXTicks || !hasData.value) return [];
  const count = chartData.value.length;
  if (count <= 8) return chartData.value.map((_, i) => i);
  // Show every Nth tick to avoid overcrowding
  const everyN = Math.ceil(count / 8);
  return chartData.value.map((_, i) => i).filter(i => i % everyN === 0 || i === count - 1);
});

// --- Hover tooltip ---------------------------------------------------------
const containerRef = ref<HTMLElement | null>(null);
const hoveredIndex = ref(-1);
const tooltipVisible = ref(false);
const tooltipLeft = ref(0);
const tooltipTop = ref(0);

function onPointerMove(e: PointerEvent): void {
  const el = containerRef.value;
  if (!el || !hasData.value) return;
  const rect = el.getBoundingClientRect();
  // jsdom reports 0-sized rects; fall back to the viewBox width so the nearest
  // index still resolves in unit tests.
  const denom = rect.width || props.width;
  const x = clamp(e.clientX - rect.left, 0, denom);
  const viewboxX = (x / denom) * props.width;
  const idx = clamp(
    Math.round((viewboxX - padding) / stepX.value),
    0,
    chartData.value.length - 1,
  );
  hoveredIndex.value = idx;
  tooltipLeft.value = clamp(x, 8, Math.max(8, rect.width - 8));
  tooltipTop.value = clamp(
    e.clientY - rect.top - 26,
    0,
    Math.max(0, rect.height - 26),
  );
  tooltipVisible.value = true;
}

function onPointerLeave(): void {
  tooltipVisible.value = false;
  hoveredIndex.value = -1;
}

const tooltipStyle = computed(() => ({
  left: `${tooltipLeft.value}px`,
  top: `${tooltipTop.value}px`,
}));

function formatValue(v: number): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatWithUnit(v: number): string {
  const valueText = formatValue(v);
  if (props.unit === "$") return `$${valueText}`;
  if (props.unit) return `${valueText} ${props.unit}`;
  return valueText;
}

const tooltipText = computed(() => {
  const idx = hoveredIndex.value;
  const v = chartData.value[idx];
  if (idx < 0 || v === undefined) return "";
  const valueText = formatWithUnit(v);
  const label = props.labels?.[idx];
  return label ? `${label}: ${valueText}` : valueText;
});

// Non-decorative chart: expose a readable summary of the series to assistive
// technology instead of leaving the svg purely decorative.
const accessibleLabel = computed(() => {
  if (!hasData.value) return t("components.Sparkline.no_data");
  const values = chartData.value.map(formatWithUnit).join(", ");
  return t("components.Sparkline.chart_series", {
    count: chartData.value.length,
    values,
  });
});
</script>
