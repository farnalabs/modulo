<template>
  <div class="page-wide" :data-loading="store.loading ? 'true' : 'false'" data-testid="analytics-view">
    <PageHeader
      :title="$t('views.AnalyticsView.analytics')"
      :subtitle="$t('views.AnalyticsView.subtitle')"
      data-testid="analytics-title"
    />

    <div
      v-if="store.flagOff"
      class="card p-6 text-center"
      data-testid="analytics-not-enabled"
    >
      <h2 class="text-base font-semibold">{{ $t('views.AnalyticsView.not_enabled') }}</h2>
      <p class="mt-1 text-sm text-muted-foreground">{{ $t('views.AnalyticsView.not_enabled_detail') }}</p>
    </div>

    <template v-else>
      <AnalyticsFilterBar
        class="mb-4"
        :filters="store.filters"
        :measure="store.measure"
        :folders="store.folders"
        :pipelines="store.pipelines"
        @update:filters="onFiltersChanged"
        @update:measure="onMeasureChanged"
      />

      <ErrorAlert
        v-if="errorMessage"
        :message="errorMessage"
        :on-retry="store.fetchQuery"
        class="mb-4"
        data-testid="analytics-error"
      />

      <div v-if="store.loading" class="card p-4" data-testid="analytics-loading">
        <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div v-for="n in 4" :key="n" class="h-20 animate-pulse rounded-lg bg-muted" />
        </div>
      </div>

      <template v-else-if="store.results">
        <div
          v-if="!store.hasData"
          class="card p-6 text-center"
          data-testid="analytics-empty-state"
        >
          <h2 class="text-base font-semibold">{{ $t('views.AnalyticsView.no_data_yet') }}</h2>
          <p v-if="store.earliestAvailableDate" class="mt-1 text-sm text-muted-foreground">
            {{ $t('views.AnalyticsView.data_since', { date: store.earliestAvailableDate }) }}
          </p>
          <p v-else class="mt-1 text-sm text-muted-foreground">
            {{ $t('views.AnalyticsView.no_data_detail') }}
          </p>
        </div>

        <div v-else class="space-y-4">
          <div class="card p-4">
            <h2 class="mb-3 text-base font-semibold">{{ $t('views.AnalyticsView.chart_title') }}</h2>
            <AnalyticsChart :series="store.buckets" :measure="store.measure" :group-by="store.groupBy" />
          </div>

          <div class="card p-4" data-testid="analytics-table">
            <div class="mb-3 flex items-center justify-between">
              <h2 class="text-base font-semibold">{{ $t('views.AnalyticsView.trend_table_title') }}</h2>
              <span class="text-xs text-muted-foreground">{{ $t('views.AnalyticsView.delta_hint') }}</span>
            </div>
            <div class="overflow-x-auto">
              <table class="w-full text-sm">
                <thead>
                  <tr class="border-b text-left text-muted-foreground">
                    <th class="pb-2 font-medium">{{ $t('views.AnalyticsView.bucket_label') }}</th>
                    <th class="pb-2 text-right font-medium">{{ currentMeasureLabel }}</th>
                    <th class="pb-2 text-right font-medium">{{ $t('views.AnalyticsView.delta') }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in tableRows" :key="row.label" class="border-b last:border-0">
                    <td class="py-2.5 font-medium tabular-nums">{{ row.label }}</td>
                    <td class="py-2.5 text-right tabular-nums">{{ row.formatted }}</td>
                    <td class="py-2.5 text-right">
                      <span
                        v-if="row.direction"
                        :class="arrowClass(row.direction)"
                        class="inline-flex items-center gap-1 tabular-nums"
                        data-testid="analytics-trend-arrow"
                      >
                        <span aria-hidden="true">{{ arrowGlyph(row.direction) }}</span>
                        <span v-if="row.deltaPercent">{{ row.deltaPercent }}</span>
                      </span>
                      <span v-else class="text-muted-foreground">—</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, watch } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import PageHeader from "../components/shared/PageHeader.vue";
import ErrorAlert from "../components/shared/ErrorAlert.vue";
import AnalyticsChart from "../components/analytics/AnalyticsChart.vue";
import AnalyticsFilterBar from "../components/analytics/AnalyticsFilterBar.vue";
import { formatApiError } from "../lib/api/formatError";
import {
  useAnalyticsStore,
  computeTrendDelta,
  formatDeltaPercent,
  formatMeasureValue,
  isDimensioned,
  aggregateByKey,
  formatBucketDate,
  measureValue,
  type AnalyticsFilters,
  type AnalyticsMeasure,
  type TrendDirection,
} from "../stores/analytics";

const { t } = useI18n();
const route = useRoute();
const store = useAnalyticsStore();

interface TableRow {
  label: string;
  formatted: string;
  direction: TrendDirection;
  deltaPercent: string | null;
}

const errorMessage = computed(() => (store.error ? formatApiError(store.error) : ""));

const currentMeasureLabel = computed(() => {
  const measure = store.measure;
  switch (measure) {
    case "count":
      return t("views.AnalyticsView.measure_count");
    case "cost":
      return t("views.AnalyticsView.measure_cost");
    case "tokens":
      return t("views.AnalyticsView.measure_tokens");
    case "duration":
      return t("views.AnalyticsView.measure_duration");
    case "success_rate":
      return t("views.AnalyticsView.measure_success_rate");
    default:
      return t("views.AnalyticsView.measure_count");
  }
});

const tableRows = computed<TableRow[]>(() => {
  const currentBuckets = store.buckets;
  const previousBuckets = store.previousResults?.buckets ?? [];
  const dimensioned = isDimensioned(currentBuckets);
  // Dimensioned queries return one bucket per (date x key): roll each window up
  // by key so the table shows one row per key with an apples-to-apples delta.
  const current = dimensioned ? aggregateByKey(currentBuckets) : currentBuckets;
  const previous = dimensioned ? aggregateByKey(previousBuckets) : previousBuckets;
  const measure = store.measure;
  return current.map((bucket, index) => {
    const label = bucket.key ?? formatBucketDate(bucket.date);
    // Windows are equal-length: match dimensioned buckets by key and
    // undimensioned buckets by offset within the window.
    const prev =
      bucket.key != null
        ? previous.find((p) => p.key === bucket.key) ?? null
        : previous[index] ?? null;
    const currentValue = measureValue(bucket, measure);
    const prevValue = prev ? measureValue(prev, measure) : null;
    return {
      label,
      formatted: formatMeasureValue(currentValue, measure),
      direction: computeTrendDelta(currentValue, prevValue),
      deltaPercent: formatDeltaPercent(currentValue, prevValue),
    };
  });
});

function onFiltersChanged(filters: AnalyticsFilters): void {
  store.setFilters(filters);
  void store.fetchQuery();
}

function onMeasureChanged(measure: AnalyticsMeasure): void {
  store.setMeasure(measure);
}

function arrowGlyph(direction: TrendDirection): string {
  if (direction === "up") return "▲";
  if (direction === "down") return "▼";
  return "→";
}

function arrowClass(direction: TrendDirection): string {
  if (direction === "up") return "text-success";
  if (direction === "down") return "text-destructive";
  return "text-muted-foreground";
}

onMounted(async () => {
  await store.fetchOptions();
  // Pre-filter from a deep link (e.g. Remy's /analytics?group_by=day&date_from=...).
  if (route.query && Object.keys(route.query).length > 0) {
    store.applyQueryParams(route.query);
  }
  await store.fetchQuery();
});

// Remy's panel is a global overlay, so a deep link can be clicked while already
// on /analytics: the component is reused, onMounted does not re-fire, and the
// pre-filter would never apply. Watch the route query and re-apply + refetch on
// same-route navigation with a new query.
watch(
  () => route.query,
  async () => {
    if (route.query && Object.keys(route.query).length > 0) {
      store.applyQueryParams(route.query);
    }
    await store.fetchQuery();
  },
);
</script>
