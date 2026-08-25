<template>
  <div class="card space-y-4 p-4" data-testid="analytics-filter-bar">
    <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <div>
        <p class="mb-2 text-xs font-medium text-muted-foreground">
          {{ $t("views.AnalyticsView.timespan") }}
        </p>
        <div class="flex flex-wrap gap-1">
          <button type="button"
            v-for="t in timespans"
            :key="t.value"
            :data-testid="`analytics-timespan-${t.value}`"
            :class="[
              'rounded px-3 py-1 text-xs font-medium transition-colors',
              filters.timespan === t.value
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:bg-muted/80',
            ]"
            @click="emitFilters({ timespan: t.value })"
          >
            {{ $t(t.labelKey) }}
          </button>
        </div>
      </div>

      <div>
        <p class="mb-2 text-xs font-medium text-muted-foreground">
          {{ $t("views.AnalyticsView.group_by") }}
        </p>
        <div class="flex flex-wrap gap-1">
          <template v-if="isHourGranular">
            <button
              type="button"
              disabled
              aria-disabled="true"
              class="cursor-not-allowed rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground opacity-70"
              data-testid="analytics-group-by-hour"
            >
              {{ $t("views.AnalyticsView.group_by_hour") }}
            </button>
          </template>
          <button type="button"
            v-else
            v-for="g in groupByOptions"
            :key="g.value"
            :data-testid="`analytics-group-by-${g.value}`"
            :class="[
              'rounded px-3 py-1 text-xs font-medium transition-colors',
              filters.groupBy === g.value
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:bg-muted/80',
            ]"
            @click="emitFilters({ groupBy: g.value })"
          >
            {{ $t(g.labelKey) }}
          </button>
        </div>
      </div>

      <div>
        <p class="mb-2 text-xs font-medium text-muted-foreground">
          {{ $t("views.AnalyticsView.measure") }}
        </p>
        <div class="flex flex-wrap gap-1">
          <button type="button"
            v-for="m in measures"
            :key="m.value"
            :data-testid="`analytics-measure-${m.value}`"
            :class="[
              'rounded px-3 py-1 text-xs font-medium transition-colors',
              measure === m.value
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:bg-muted/80',
            ]"
            @click="emit('update:measure', m.value)"
          >
            {{ $t(m.labelKey) }}
          </button>
        </div>
      </div>

      <label class="block">
        <span class="mb-2 block text-xs font-medium text-muted-foreground">
          {{ $t("views.AnalyticsView.dimension") }}
        </span>
        <select
          :value="filters.dimension ?? ''"
          class="w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm"
          data-testid="analytics-filter-dimension"
          @change="onDimensionChange"
        >
          <option value="">{{ $t("views.AnalyticsView.dimension_none") }}</option>
          <option
            v-for="d in dimensions"
            :key="d.value"
            :value="d.value"
          >
            {{ $t(d.labelKey) }}
          </option>
        </select>
      </label>
    </div>

    <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <label class="block">
        <span class="text-xs font-medium text-muted-foreground">{{ $t("views.AnalyticsView.filter_folder") }}</span>
        <select
          :value="filters.folderId ?? ''"
          class="mt-1 w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm"
          data-testid="analytics-filter-folder"
          @change="onFolderChange"
        >
          <option value="">{{ $t("views.AnalyticsView.all") }}</option>
          <option v-for="f in folders" :key="f.id" :value="f.id">{{ f.name }}</option>
        </select>
      </label>

      <label class="block">
        <span class="text-xs font-medium text-muted-foreground">{{ $t("views.AnalyticsView.filter_pipeline") }}</span>
        <select
          :value="filters.pipelineId ?? ''"
          class="mt-1 w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm"
          data-testid="analytics-filter-pipeline"
          @change="onPipelineChange"
        >
          <option value="">{{ $t("views.AnalyticsView.all") }}</option>
          <option v-for="p in pipelines" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>
      </label>

      <label class="block">
        <span class="text-xs font-medium text-muted-foreground">{{ $t("views.AnalyticsView.filter_trigger_type") }}</span>
        <select
          :value="filters.triggerType ?? ''"
          class="mt-1 w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm"
          data-testid="analytics-filter-trigger-type"
          @change="onTriggerTypeChange"
        >
          <option value="">{{ $t("views.AnalyticsView.all") }}</option>
          <option v-for="t in triggerTypes" :key="t" :value="t">{{ t }}</option>
        </select>
      </label>

      <label class="block">
        <span class="text-xs font-medium text-muted-foreground">{{ $t("views.AnalyticsView.filter_status") }}</span>
        <select
          :value="filters.status ?? ''"
          class="mt-1 w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm"
          data-testid="analytics-filter-status"
          @change="onStatusChange"
        >
          <option value="">{{ $t("views.AnalyticsView.all") }}</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>

      <label class="block">
        <span class="text-xs font-medium text-muted-foreground">{{ $t("views.AnalyticsView.filter_error_code") }}</span>
        <input
          :value="filters.errorCode ?? ''"
          type="text"
          class="mt-1 w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm"
          data-testid="analytics-filter-error-code"
          :placeholder="$t('views.AnalyticsView.filter_error_code_placeholder')"
          @input="onErrorCodeInput"
        />
      </label>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import {
  MEASURES,
  RUN_STATUSES,
  TIMESPANS,
  TRIGGER_TYPES,
  type AnalyticsDimension,
  type AnalyticsFilters,
  type AnalyticsMeasure,
  type OptionItem,
} from "../../stores/analytics";

const props = defineProps<{
  filters: AnalyticsFilters;
  measure: AnalyticsMeasure;
  folders: OptionItem[];
  pipelines: OptionItem[];
}>();

const emit = defineEmits<{
  "update:filters": [filters: AnalyticsFilters];
  "update:measure": [measure: AnalyticsMeasure];
}>();

const timespans = TIMESPANS.map((t) => ({
  value: t.value,
  labelKey: `views.AnalyticsView.timespan_${t.value}`,
}));

// Hour-granular timespans (1h/24h) force `group_by=hour` server-side, so the
// day/week granularity control is inert. Reflect the applied granularity and
// disable the control instead of letting it silently ignore clicks.
const isHourGranular = computed(() => {
  const timespan = TIMESPANS.find((t) => t.value === props.filters.timespan);
  return timespan?.granularity === "hour";
});

const groupByOptions = [
  { value: "day" as const, labelKey: "views.AnalyticsView.group_by_day" },
  { value: "week" as const, labelKey: "views.AnalyticsView.group_by_week" },
];

const dimensions: { value: AnalyticsDimension; labelKey: string }[] = [
  { value: "trigger_type", labelKey: "views.AnalyticsView.dimension_trigger_type" },
  { value: "status", labelKey: "views.AnalyticsView.dimension_status" },
  { value: "pipeline", labelKey: "views.AnalyticsView.dimension_pipeline" },
  { value: "folder", labelKey: "views.AnalyticsView.dimension_folder" },
  { value: "team", labelKey: "views.AnalyticsView.dimension_team" },
  { value: "error_code", labelKey: "views.AnalyticsView.dimension_error_code" },
];

const measures = MEASURES;
const triggerTypes = TRIGGER_TYPES;
const statuses = RUN_STATUSES;

function selectValue(e: Event): string {
  return (e.target as HTMLSelectElement).value;
}

function emitFilters(patch: Partial<AnalyticsFilters>): void {
  emit("update:filters", { ...props.filters, ...patch });
}

function onDimensionChange(e: Event): void {
  const v = selectValue(e);
  emitFilters({ dimension: (v || null) as AnalyticsDimension | null });
}

function onFolderChange(e: Event): void {
  emitFilters({ folderId: selectValue(e) || null });
}

function onPipelineChange(e: Event): void {
  emitFilters({ pipelineId: selectValue(e) || null });
}

function onTriggerTypeChange(e: Event): void {
  emitFilters({ triggerType: selectValue(e) || null });
}

function onStatusChange(e: Event): void {
  emitFilters({ status: selectValue(e) || null });
}

function onErrorCodeInput(e: Event): void {
  const value = (e.target as HTMLInputElement).value.trim();
  emitFilters({ errorCode: value || null });
}
</script>
