import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { parseISO, isValid } from "date-fns";
import { api } from "../lib/api/client";
import { withTimeout } from "../lib/asyncUtils";
import { toProblemDetail, type ProblemDetail } from "../lib/api/formatError";
import { formatDateShortWithTime } from "../lib/formatDate";

export type AnalyticsMeasure =
  | "count"
  | "cost"
  | "tokens"
  | "duration"
  | "success_rate";
export type AnalyticsTimespan = "1h" | "24h" | "3d" | "7d" | "30d" | "90d";
export type AnalyticsGroupBy = "day" | "week" | "hour";
export type AnalyticsDimension =
  | "trigger_type"
  | "status"
  | "pipeline"
  | "folder"
  | "team"
  | "error_code";
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
  /** FAR-200 freshness indicator: hours since the newest day with a terminal
   * fact row, and whether that lags > ~36h. Surfaced as a stale-data notice.
   */
  facts_freshness_hours?: number | null;
  facts_stale?: boolean;
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
  /** Run failure code filter (e.g. `executor_stalled`) from a deep link or the
   * filter bar. Round-trips through the backend's `error_code` query param.
   */
  errorCode?: string | null;
  /** Explicit range override from a deep link (e.g. /analytics?date_from=...).
   * When both are set they win over the timespan derivation in serializeFilters.
   */
  dateFrom?: string;
  dateTo?: string;
}

export interface AnalyticsQueryParams {
  group_by: string;
  dimension?: string;
  trigger_type?: string;
  status?: string;
  pipeline_id?: string;
  folder_id?: string;
  error_code?: string;
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
  errorCode: null,
};

export const TRIGGER_TYPES = [
  "manual",
  "webhook",
  "cron",
  "polling",
  "agent_signal",
  "ongoing",
  "correction",
] as const;

export const RUN_STATUSES = [
  "pending",
  "running",
  "awaiting_human",
  "claimed",
  "complete",
  "failed",
  "cancelled",
  "eval_failed",
  "stalled",
  "budget_exceeded",
  "router_no_match",
] as const;

export const TIMESPANS: AnalyticsTimespanOption[] = [
  { value: "1h", hours: 1, granularity: "hour" },
  { value: "24h", days: 1, granularity: "hour" },
  { value: "3d", days: 3, granularity: "day" },
  { value: "7d", days: 7 },
  { value: "30d", days: 30 },
  { value: "90d", days: 90 },
];

export interface AnalyticsTimespanOption {
  value: AnalyticsTimespan;
  days?: number;
  hours?: number;
  granularity?: "hour" | "day" | "week";
}

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
const HOUR_MS = 3600000;

function isoDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function parseDay(value: string): Date {
  const parsed = parseISO(`${value}T00:00:00.000Z`);
  if (!isValid(parsed)) {
    throw new Error(`Invalid day value: ${value}`);
  }
  return parsed;
}

/**
 * Shift a UTC day by a whole number of days. Epoch arithmetic keeps the result at
 * UTC midnight regardless of local timezone. The epoch is derived from an existing
 * valid Date minus integer days, so it can never be an Invalid Date.
 */
function shiftUtcDays(date: Date, days: number): Date {
  return new Date(date.getTime() - days * DAY_MS); // nosemgrep: new-date-without-guard
}

/**
 * Rolling timespan → typed query params (UTC). Filters included only when set.
 * Hour-granular timespans (1h/24h) send ISO datetime strings and force
 * `group_by=hour`; day-granular timespans keep the day-window behaviour (ISO
 * day strings) and respect the user's day/week granularity control.
 */
export function serializeFilters(
  filters: AnalyticsFilters,
  now: Date = new Date(),
): AnalyticsQueryParams {
  const timespan =
    TIMESPANS.find((t) => t.value === filters.timespan) ??
    TIMESPANS.find((t) => t.value === DEFAULT_FILTERS.timespan)!;
  let groupBy: string;
  let dateFrom: string;
  let dateTo: string;
  if (filters.dateFrom && filters.dateTo) {
    // Explicit range from a deep link (e.g. Remy's /analytics?date_from=...):
    // send the range verbatim and respect the carried granularity.
    groupBy = filters.groupBy;
    dateFrom = filters.dateFrom;
    dateTo = filters.dateTo;
  } else if (timespan.granularity === "hour") {
    // Hour-granular window: derive the span from the preset (`hours` for 1h,
    // `days * 24` for 24h) and send ISO datetimes so the backend grids by hour.
    const spanHours = timespan.hours ?? (timespan.days ?? 1) * 24;
    groupBy = "hour";
    dateFrom = new Date(now.getTime() - spanHours * HOUR_MS).toISOString(); // nosemgrep: new-date-without-guard
    dateTo = now.toISOString();
  } else {
    // Parse the ISO day as UTC (parseDay appends T00:00:00.000Z). Parsing the bare
    // date string with parseISO uses local midnight, which round-trips through
    // toISOString() to the previous UTC day on any UTC+ timezone.
    const dayTo = parseDay(isoDay(now));
    const dayFrom = shiftUtcDays(dayTo, timespan.days ?? 0);
    groupBy = filters.groupBy;
    dateFrom = isoDay(dayFrom);
    dateTo = isoDay(dayTo);
  }
  const params: AnalyticsQueryParams = {
    group_by: groupBy,
    date_from: dateFrom,
    date_to: dateTo,
    limit: 1000,
  };
  if (filters.dimension) params.dimension = filters.dimension;
  if (filters.triggerType) params.trigger_type = filters.triggerType;
  if (filters.status) params.status = filters.status;
  if (filters.pipelineId) params.pipeline_id = filters.pipelineId;
  if (filters.folderId) params.folder_id = filters.folderId;
  if (filters.errorCode) params.error_code = filters.errorCode;
  return params;
}

function firstQueryParam(value: unknown): string | null {
  if (typeof value === "string" && value) return value;
  if (Array.isArray(value)) {
    const first = value.find((v) => typeof v === "string" && v);
    return typeof first === "string" ? first : null;
  }
  return null;
}

const GROUP_BY_VALUES: AnalyticsGroupBy[] = ["day", "week", "hour"];
const DIMENSION_VALUES: AnalyticsDimension[] = [
  "trigger_type",
  "status",
  "pipeline",
  "folder",
  "team",
  "error_code",
];

/**
 * Map an /analytics URL query (from a deep link such as Remy's
 * `/analytics?group_by=day&date_from=...&pipeline_id=...`) onto the store's
 * filter state. Returns true when at least one filter was applied.
 * Explicit date ranges are kept as an override so the query round-trips
 * exactly; changing the timespan later returns to timespan-derived ranges.
 */
export function applyQueryParamsToFilters(
  query: Record<string, unknown>,
  current: AnalyticsFilters,
): { filters: AnalyticsFilters; applied: boolean } {
  const patch: Partial<AnalyticsFilters> = {};
  const groupBy = firstQueryParam(query.group_by);
  if (groupBy && (GROUP_BY_VALUES as string[]).includes(groupBy)) {
    patch.groupBy = groupBy as AnalyticsGroupBy;
  }
  const dimension = firstQueryParam(query.dimension);
  if (dimension && (DIMENSION_VALUES as string[]).includes(dimension)) {
    patch.dimension = dimension as AnalyticsDimension;
  }
  const triggerType = firstQueryParam(query.trigger_type);
  if (triggerType) patch.triggerType = triggerType;
  const status = firstQueryParam(query.status);
  if (status) patch.status = status;
  const pipelineId = firstQueryParam(query.pipeline_id);
  if (pipelineId) patch.pipelineId = pipelineId;
  const folderId = firstQueryParam(query.folder_id);
  if (folderId) patch.folderId = folderId;
  const errorCode = firstQueryParam(query.error_code);
  if (errorCode) patch.errorCode = errorCode;
  const dateFrom = firstQueryParam(query.date_from);
  const dateTo = firstQueryParam(query.date_to);
  if (dateFrom && dateTo && isValid(parseISO(dateFrom)) && isValid(parseISO(dateTo))) {
    patch.dateFrom = dateFrom;
    patch.dateTo = dateTo;
  }
  const applied = Object.keys(patch).length > 0;
  return { filters: applied ? { ...current, ...patch } : current, applied };
}

/**
 * Shift a window back by exactly one window (for current-vs-previous deltas).
 * Hour-granular windows (params carry ISO datetimes) are shifted back by their
 * span in hours; day-granular windows keep the existing day-shift logic.
 */
export function previousWindowParams(params: AnalyticsQueryParams): AnalyticsQueryParams {
  if (params.date_from.includes("T") || params.date_to.includes("T")) {
    const to = parseISO(params.date_to);
    const from = parseISO(params.date_from);
    if (!isValid(to) || !isValid(from)) {
      return { ...params };
    }
    const spanMs = to.getTime() - from.getTime();
    const prevTo = new Date(to.getTime() - spanMs); // nosemgrep: new-date-without-guard
    const prevFrom = new Date(from.getTime() - spanMs); // nosemgrep: new-date-without-guard
    return {
      ...params,
      date_from: prevFrom.toISOString(),
      date_to: prevTo.toISOString(),
    };
  }
  const to = parseDay(params.date_to);
  const from = parseDay(params.date_from);
  const spanDays = Math.round((to.getTime() - from.getTime()) / DAY_MS) + 1;
  const prevTo = shiftUtcDays(from, 1);
  const prevFrom = shiftUtcDays(prevTo, spanDays - 1);
  return { ...params, date_from: isoDay(prevFrom), date_to: isoDay(prevTo) };
}

export function measureValue(
  bucket: AnalyticsBucket,
  measure: AnalyticsMeasure,
): number | null {
  const raw = bucket[MEASURE_KEYS[measure]];
  return typeof raw === "number" ? raw : null;
}

/**
 * Roll up per-(date, dimension-key) backend buckets into one summary bucket per
 * key. The backend returns one bucket per (grid time x dim key), so a dimensioned
 * series has many buckets per key; the chart/table render one entry per key.
 * Counts/cost/tokens are summed; avg_duration_ms and success_rate are weighted by
 * count (mirroring the backend's own duration weighting in bucket_rows).
 */
export function aggregateByKey(buckets: AnalyticsBucket[]): AnalyticsBucket[] {
  const groups = new Map<string, AnalyticsBucket & { durSum: number; durN: number; rateSum: number; rateN: number }>();
  for (const b of buckets) {
    const groupKey = b.key ?? "";
    let g = groups.get(groupKey);
    if (!g) {
      g = {
        date: b.date,
        key: b.key ?? null,
        count: 0,
        durSum: 0,
        durN: 0,
        rateSum: 0,
        rateN: 0,
      };
      groups.set(groupKey, g);
    }
    g.count += b.count;
    if (typeof b.total_cost_usd === "number") {
      g.total_cost_usd = (g.total_cost_usd ?? 0) + b.total_cost_usd;
    }
    if (typeof b.total_tokens === "number") {
      g.total_tokens = (g.total_tokens ?? 0) + b.total_tokens;
    }
    if (typeof b.avg_duration_ms === "number" && b.count > 0) {
      g.durSum += b.avg_duration_ms * b.count;
      g.durN += b.count;
    }
    if (typeof b.success_rate === "number" && b.count > 0) {
      g.rateSum += b.success_rate * b.count;
      g.rateN += b.count;
    }
  }
  return [...groups.values()].map((g) => ({
    date: g.date,
    key: g.key,
    count: g.count,
    total_cost_usd: g.total_cost_usd,
    total_tokens: g.total_tokens,
    avg_duration_ms: g.durN > 0 ? g.durSum / g.durN : null,
    success_rate: g.rateN > 0 ? g.rateSum / g.rateN : null,
  }));
}

/** True when a series carries dimension keys (backend emits per-key buckets). */
export function isDimensioned(series: AnalyticsBucket[]): boolean {
  return series.some((b) => b.key != null && b.key !== "");
}

/**
 * Humanize a backend bucket label. Hour-granular buckets carry naive-UTC ISO
 * datetimes (e.g. "2026-08-06T14:00:00") that must be read as UTC — appending Z
 * so `new Date` parses them as UTC, then formatting in the viewer's timezone.
 * Day-granular buckets are bare "YYYY-MM-DD" strings and are returned unchanged.
 */
export function formatBucketDate(date: string | null | undefined): string {
  if (date == null) return "";
  if (date.includes("T")) {
    return formatDateShortWithTime(`${date}Z`);
  }
  return date;
}

/** Pure series → ECharts option mapping. The backend is the sole bucketing authority. */
export function buildChartOption(
  series: AnalyticsBucket[],
  measure: AnalyticsMeasure,
  _groupBy: string,
  dimension?: string | null,
  labelFormatter?: (key: string) => string,
): Record<string, unknown> {
  const dimensioned = isDimensioned(series);
  const buckets = dimensioned ? aggregateByKey(series) : series;
  const labels = buckets.map((b) => {
    const raw = b.key ?? formatBucketDate(b.date);
    return b.key != null && dimension === "error_code" && labelFormatter ? labelFormatter(b.key) : raw;
  });
  const values = buckets.map((b) => measureValue(b, measure));
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
      return `${(value * 100).toFixed(1)}%`;
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
// typed client's path-union never sees an unknown route. The client is resolved
// lazily at call time (not module scope) so importing the store never touches
// `api` — specs that mock the client without an `api` object still import fine.
type RawGet = (
  url: string,
  options?: unknown,
) => Promise<{ data?: unknown; error?: unknown }>;
function rawGet(url: string, options?: unknown): ReturnType<RawGet> {
  return (api.GET as unknown as RawGet)(url, options);
}

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
  const factsStale = computed(() => Boolean(results.value?.facts_stale));
  const factsFreshnessHours = computed(() => results.value?.facts_freshness_hours ?? null);
  const hasData = computed(() =>
    buckets.value.some(
      (b) =>
        b.count > 0 ||
        (b.total_cost_usd != null && b.total_cost_usd > 0) ||
        (b.total_tokens != null && b.total_tokens > 0),
    ),
  );
  const groupBy = computed(() => {
    if (filters.value.dateFrom && filters.value.dateTo) {
      return filters.value.groupBy;
    }
    const timespan = TIMESPANS.find((t) => t.value === filters.value.timespan);
    return timespan?.granularity === "hour" ? "hour" : filters.value.groupBy;
  });

  function setFilters(patch: Partial<AnalyticsFilters>): void {
    // Only a timespan VALUE change returns to timespan-derived ranges. The
    // filter bar always includes the current timespan in every emitted patch,
    // so comparing values (not key presence) keeps the explicit deep-link
    // range intact when the user tweaks another filter.
    const next = { ...filters.value, ...patch };
    if ("timespan" in patch && patch.timespan !== filters.value.timespan) {
      next.dateFrom = undefined;
      next.dateTo = undefined;
    }
    filters.value = next;
  }

  function setMeasure(value: AnalyticsMeasure): void {
    measure.value = value;
  }

  function resetFilters(): void {
    filters.value = { ...DEFAULT_FILTERS };
  }

  function applyQueryParams(query: Record<string, unknown>): boolean {
    const { filters: next, applied } = applyQueryParamsToFilters(query, filters.value);
    if (applied) {
      filters.value = next;
    }
    return applied;
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

  // Monotonic request token: rapid filter changes issue concurrent queries, and
  // only the latest request may commit results (out-of-order resolution must not
  // overwrite a newer window with an older one).
  let requestToken = 0;

  async function fetchQuery(): Promise<void> {
    loading.value = true;
    error.value = null;
    flagOff.value = false;
    const token = ++requestToken;
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
      if (token !== requestToken) return; // a newer request superseded this one
      results.value = current;
      previousResults.value = previous;
      earliestAvailableDate.value = deriveEarliestDate(current.buckets);
    } catch (e: unknown) {
      if (token !== requestToken) return;
      const problem = toProblemDetail(e);
      error.value = problem;
      flagOff.value = problem.status === 402;
    } finally {
      if (token === requestToken) loading.value = false;
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
    factsStale,
    factsFreshnessHours,
    hasData,
    groupBy,
    setFilters,
    setMeasure,
    resetFilters,
    applyQueryParams,
    fetchQuery,
    fetchOptions,
  };
});

function formatApiErrorMessage(e: unknown): string {
  return toProblemDetail(e).detail;
}
