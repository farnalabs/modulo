import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { api } from "../lib/api/client";
import { withTimeout } from "../lib/asyncUtils";
import { toProblemDetail, type ProblemDetail } from "../lib/api/formatError";

export type AnalyticsMeasure =
  | "count"
  | "cost"
  | "tokens"
  | "duration"
  | "success_rate";
export type AnalyticsTimespan = "24h" | "7d" | "30d" | "90d";
export type AnalyticsGroupBy = "day" | "week";
export type AnalyticsDimension =
  | "trigger_type"
  | "status"
  | "pipeline"
  | "folder"
  | "team";
export type TrendDirection = "up" | "down" | "flat" | null;

export interface AnalyticsBucket {
  date: string;
  key?: string | null;
  count: number;
  total_cost_usd?: number | null;
  total_tokens?: number | null;
  avg_duration_ms?: number | null;
  success_rate?: number | null;
}

export interface AnalyticsResponse {
  group_by: string;
  dimension?: string | null;
  date_from?: string | null;
  date_to?: string | null;
  buckets: AnalyticsBucket[];
}

export interface AnalyticsFilters {
  timespan: AnalyticsTimespan;
  groupBy: AnalyticsGroupBy;
  dimension?: AnalyticsDimension | null;
  triggerType?: string | null;
  status?: string | null;
  pipelineId?: string | null;
  folderId?: string | null;
}

export interface AnalyticsQueryParams {
  group_by: string;
  dimension?: string;
  trigger_type?: string;
  status?: string;
  pipeline_id?: string;
  folder_id?: string;
  date_from: string;
  date_to: string;
  limit: number;
}

export interface OptionItem {
  id: string;
  name: string;
}

export const DEFAULT_FILTERS: AnalyticsFilters = {
  timespan: "7d",
  groupBy: "day",
  dimension: null,
  triggerType: null,
  status: null,
  pipelineId: null,
  folderId: null,
};

export const TRIGGER_TYPES = [
  "manual",
  "webhook",
  "cron",
  "polling",
  "agent_signal",
  "correction",
] as const;

export const RUN_STATUSES = [
  "pending",
  "running",
  "awaiting_human",
  "claimed",
  "waiting_for_lock",
  "complete",
  "failed",
  "cancelled",
  "eval_failed",
] as const;

export const TIMESPANS: { value: AnalyticsTimespan; days: number }[] = [
  { value: "24h", days: 1 },
  { value: "7d", days: 7 },
  { value: "30d", days: 30 },
  { value: "90d", days: 90 },
];

export const MEASURES: { value: AnalyticsMeasure; labelKey: string }[] = [
  { value: "count", labelKey: "views.AnalyticsView.measure_count" },
  { value: "cost", labelKey: "views.AnalyticsView.measure_cost" },
  { value: "tokens", labelKey: "views.AnalyticsView.measure_tokens" },
  { value: "duration", labelKey: "views.AnalyticsView.measure_duration" },
  { value: "success_rate", labelKey: "views.AnalyticsView.measure_success_rate" },
];

const MEASURE_KEYS: Record<AnalyticsMeasure, keyof AnalyticsBucket> = {
  count: "count",
  cost: "total_cost_usd",
  tokens: "total_tokens",
  duration: "avg_duration_ms",
  success_rate: "success_rate",
};

const DAY_MS = 86400000;

function isoDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function parseDay(value: string): Date {
  return new Date(`${value}T00:00:00.000Z`);
}

/** Rolling timespan → typed query params (UTC). Filters included only when set. */
export function serializeFilters(
  filters: AnalyticsFilters,
  now: Date = new Date(),
): AnalyticsQueryParams {
  const timespan = TIMESPANS.find((t) => t.value === filters.timespan) ?? TIMESPANS[1];
  const dateTo = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  const dateFrom = new Date(dateTo.getTime() - timespan.days * DAY_MS);
  const params: AnalyticsQueryParams = {
    group_by: filters.groupBy,
    date_from: isoDay(dateFrom),
    date_to: isoDay(dateTo),
    limit: 1000,
  };
  if (filters.dimension) params.dimension = filters.dimension;
  if (filters.triggerType) params.trigger_type = filters.triggerType;
  if (filters.status) params.status = filters.status;
  if (filters.pipelineId) params.pipeline_id = filters.pipelineId;
  if (filters.folderId) params.folder_id = filters.folderId;
  return params;
}

/** Shift a window back by exactly one window (for current-vs-previous deltas). */
export function previousWindowParams(params: AnalyticsQueryParams): AnalyticsQueryParams {
  const to = parseDay(params.date_to);
  const from = parseDay(params.date_from);
  const spanDays = Math.round((to.getTime() - from.getTime()) / DAY_MS) + 1;
  const prevTo = new Date(from.getTime() - DAY_MS);
  const prevFrom = new Date(prevTo.getTime() - (spanDays - 1) * DAY_MS);
  return { ...params, date_from: isoDay(prevFrom), date_to: isoDay(prevTo) };
}

export function measureValue(
  bucket: AnalyticsBucket,
  measure: AnalyticsMeasure,
): number | null {
  const raw = bucket[MEASURE_KEYS[measure]];
  return typeof raw === "number" ? raw : null;
}

/** Pure series → ECharts option mapping. The backend is the sole bucketing authority. */
export function buildChartOption(
  series: AnalyticsBucket[],
  measure: AnalyticsMeasure,
  _groupBy: string,
): Record<string, unknown> {
  const dimensioned = series.some((b) => b.key != null && b.key !== "");
  const labels = series.map((b) => b.key ?? b.date);
  const values = series.map((b) => measureValue(b, measure));
  return {
    tooltip: { trigger: "axis" },
    grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
    xAxis: { type: "category", data: labels },
    yAxis: { type: "value" },
    series: [
      {
        name: measure,
        type: dimensioned ? "bar" : "line",
        smooth: !dimensioned,
        connectNulls: false,
        data: values,
        itemStyle: dimensioned ? { borderRadius: [3, 3, 0, 0] } : undefined,
      },
    ],
  };
}

/** Trend arrow: prev=0 or both-zero → null; current<prev → down; else up/flat. */
export function computeTrendDelta(
  current: number | null | undefined,
  previous: number | null | undefined,
): TrendDirection {
  if (current == null || previous == null) return null;
  if (previous === 0) return null;
  if (current === 0 && previous === 0) return null;
  if (current > previous) return "up";
  if (current < previous) return "down";
  return "flat";
}

/** Signed percentage delta with 1 decimal place, or null when not computable. */
export function formatDeltaPercent(
  current: number | null | undefined,
  previous: number | null | undefined,
): string | null {
  if (current == null || previous == null || previous === 0) return null;
  if (current === 0 && previous === 0) return null;
  const pct = ((current - previous) / previous) * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

export function formatMeasureValue(
  value: number | null | undefined,
  measure: AnalyticsMeasure,
): string {
  if (value == null) return "—";
  switch (measure) {
    case "cost":
      return `$${value.toFixed(2)}`;
    case "tokens":
      return value.toLocaleString();
    case "duration":
      return `${Math.round(value)}ms`;
    case "success_rate":
      return `${value.toFixed(1)}%`;
    default:
      return String(Math.round(value));
  }
}

export function deriveEarliestDate(buckets: AnalyticsBucket[] | null | undefined): string | null {
  if (!Array.isArray(buckets) || buckets.length === 0) return null;
  for (const b of buckets) {
    if (b.count > 0 || b.total_cost_usd != null || b.total_tokens != null) return b.date;
  }
  return null;
}

function validateResponse(data: unknown): data is AnalyticsResponse {
  if (!data || typeof data !== "object") return false;
  const d = data as Record<string, unknown>;
  return typeof d.group_by === "string" && Array.isArray(d.buckets);
}

// The analytics endpoint lands in the generated OpenAPI client only after the
// schema is regenerated; until then call it through an untyped alias so the
// typed client's path-union never sees an unknown route.
type RawGet = (
  url: string,
  options?: unknown,
) => Promise<{ data?: unknown; error?: unknown }>;
const rawGet = api.GET as unknown as RawGet;

export const useAnalyticsStore = defineStore("analytics", () => {
  const filters = ref<AnalyticsFilters>({ ...DEFAULT_FILTERS });
  const measure = ref<AnalyticsMeasure>("count");
  const results = ref<AnalyticsResponse | null>(null);
  const previousResults = ref<AnalyticsResponse | null>(null);
  const folders = ref<OptionItem[]>([]);
  const pipelines = ref<OptionItem[]>([]);
  const loading = ref(false);
  const optionsLoading = ref(false);
  const error = ref<string | ProblemDetail | null>(null);
  const flagOff = ref(false);
  const earliestAvailableDate = ref<string | null>(null);

  const buckets = computed(() => results.value?.buckets ?? []);
  const hasData = computed(() => buckets.value.some((b) => b.count > 0));
  const groupBy = computed(() => filters.value.groupBy);

  function setFilters(patch: Partial<AnalyticsFilters>): void {
    filters.value = { ...filters.value, ...patch };
  }

  function setMeasure(value: AnalyticsMeasure): void {
    measure.value = value;
  }

  function resetFilters(): void {
    filters.value = { ...DEFAULT_FILTERS };
  }

  async function fetchWindow(params: AnalyticsQueryParams): Promise<AnalyticsResponse> {
    const { data, error: err } = await withTimeout(
      rawGet("/api/v1/analytics/query", { params: { query: params } }),
      15000,
      "Analytics query request",
    );
    if (err) throw err;
    if (!validateResponse(data)) {
      throw new Error("Received invalid analytics data from server.");
    }
    return data;
  }

  async function fetchOptions(): Promise<void> {
    if (optionsLoading.value) return;
    optionsLoading.value = true;
    try {
      const [foldersRes, pipelinesRes] = await Promise.all([
        withTimeout(api.GET("/api/v1/pipeline-folders"), 15000, "Analytics folders request"),
        withTimeout(
          api.GET("/api/v1/pipelines", { params: { query: { page_size: 100 } } }),
          15000,
          "Analytics pipelines request",
        ),
      ]);
      if (!foldersRes.error && Array.isArray(foldersRes.data)) {
        folders.value = foldersRes.data.map((f: OptionItem) => ({ id: f.id, name: f.name }));
      }
      const pipelineItems = pipelinesRes.data?.items;
      if (!pipelinesRes.error && Array.isArray(pipelineItems)) {
        pipelines.value = pipelineItems.map((p: OptionItem) => ({ id: p.id, name: p.name }));
      }
    } catch (e: unknown) {
      // Options are non-critical: leave the selects empty and let the query run unfiltered.
      console.warn("[analytics] failed to load filter options:", formatApiErrorMessage(e));
    } finally {
      optionsLoading.value = false;
    }
  }

  async function fetchQuery(): Promise<void> {
    if (loading.value) return;
    loading.value = true;
    error.value = null;
    flagOff.value = false;
    try {
      const params = serializeFilters(filters.value);
      const current = await fetchWindow(params);
      let previous: AnalyticsResponse | null = null;
      try {
        previous = await fetchWindow(previousWindowParams(params));
      } catch (e: unknown) {
        // The previous window is best-effort: a failure there must not hide the current series.
        console.warn("[analytics] failed to load previous window:", formatApiErrorMessage(e));
      }
      results.value = current;
      previousResults.value = previous;
      earliestAvailableDate.value = deriveEarliestDate(current.buckets);
    } catch (e: unknown) {
      const problem = toProblemDetail(e);
      error.value = problem;
      flagOff.value = problem.status === 402;
    } finally {
      loading.value = false;
    }
  }

  return {
    filters,
    measure,
    results,
    previousResults,
    folders,
    pipelines,
    loading,
    optionsLoading,
    error,
    flagOff,
    earliestAvailableDate,
    buckets,
    hasData,
    groupBy,
    setFilters,
    setMeasure,
    resetFilters,
    fetchQuery,
    fetchOptions,
  };
});

function formatApiErrorMessage(e: unknown): string {
  return toProblemDetail(e).detail;
}
