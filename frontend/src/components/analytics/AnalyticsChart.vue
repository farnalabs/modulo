<template>
  <div class="h-72 w-full" data-testid="analytics-chart">
    <component
      :is="ChartRenderer"
      v-if="series.length > 0"
      :option="chartOption"
      autoresize
      class="h-full w-full"
      data-testid="analytics-chart-canvas"
    />
    <div
      v-else
      class="flex h-full items-center justify-center rounded-lg border border-dashed border-border text-sm text-muted-foreground"
      data-testid="analytics-chart-empty"
    >
      {{ $t("views.AnalyticsView.no_chart_data") }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent } from "vue";
import { buildChartOption, type AnalyticsBucket, type AnalyticsMeasure } from "../../stores/analytics";

const props = defineProps<{
  series: AnalyticsBucket[];
  measure: AnalyticsMeasure;
  groupBy: string;
}>();

const ChartRenderer = defineAsyncComponent(async () => {
  await import("echarts");
  const { default: VChart } = await import("vue-echarts");
  return VChart;
});

const chartOption = computed(() =>
  buildChartOption(props.series, props.measure, props.groupBy),
);
</script>
