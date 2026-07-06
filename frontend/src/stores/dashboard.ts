import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { useI18n } from "vue-i18n";
import { api } from "../lib/api/client";
import { withTimeout } from "../lib/asyncUtils";
import { registerHandler } from "./syncRegistry";
import { formatApiError, type ProblemDetail } from "../lib/api/formatError";
import type { EventBusEvent } from "@/types/events";

interface TeamMetrics {
  id: string;
  name: string;
  total_runs: number;
  active_pipelines: number;
  run_counts_by_status: {
    running: number;
    awaiting_human: number;
    failed: number;
    idle: number;
  };
  eval_pass_rate?: {
    total_evals: number;
    passed_evals: number;
    pass_rate: number;
  };
}

interface TrendDay {
  date: string;
  run_count: number;
  eval_pass_rate: number | null;
  token_spend_usd: number;
}

interface RecentRun {
  id: string;
  pipeline_name: string;
  status: string;
  created_at: string;
  trigger_type: string;
}

export interface ConfigWarning {
  type: string;
  severity: string;
  message: string;
  action_label: string;
  action_url: string;
}

export interface DashboardSummary {
  total_runs: number;
  active_pipelines: number;
  run_counts_by_status: {
    running: number;
    awaiting_human: number;
    failed: number;
    idle: number;
  };
  teams: TeamMetrics[];
  eval_pass_rate: {
    overall_pass_rate: number;
    total_evals: number;
    passed_evals: number;
    per_pipeline: Record<
      string,
      { total_evals: number; passed_evals: number; pass_rate: number }
    >;
    per_team_pipeline: Record<
      string,
      Record<
        string,
        { total_evals: number; passed_evals: number; pass_rate: number }
      >
    >;
  } | null;
  trend: TrendDay[];
  recent_runs: RecentRun[];
  config_warnings: ConfigWarning[];
}

function validateDashboardSummary(data: unknown): DashboardSummary | null {
  if (!data || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  const required = ["total_runs", "active_pipelines", "run_counts_by_status", "teams", "trend", "recent_runs"];
  for (const key of required) {
    if (d[key] == null) return null;
  }
  if (!Array.isArray(d.teams)) return null;
  if (!Array.isArray(d.trend)) return null;
  if (!Array.isArray(d.recent_runs)) return null;
  return d as unknown as DashboardSummary;
}

interface TrendsResponse {
  days: number;
  run_counts: Array<{ date: string; run_count: number }>;
  eval_pass_rates: Array<{
    date: string;
    total_evals: number;
    passed_evals: number;
    pass_rate: number | null;
  }>;
  token_spend: Array<{ date: string; total_spend_usd: number }>;
  hitl_volume: Array<{
    date: string;
    total_decisions: number;
    approved_count: number;
    rejected_count: number;
    rejection_rate: number;
    avg_time_to_approve_ms: number | null;
  }>;
  rejection_trend: Array<{
    date: string;
    rolling_rejection_rate: number;
    raw_rejection_rate: number;
  }>;
  correlation: Array<{
    date: string;
    rejection_rate: number;
    eval_pass_rate: number | null;
  }>;
  feedback_volume: Array<{
    date: string;
    feedback_count: number;
    resolved_count: number;
    correcting_count: number;
  }>;
}

export const useDashboardStore = defineStore("dashboard", () => {
  const { t } = useI18n();
  const summary = ref<DashboardSummary | null>(null);
  const loading = ref(false);
  const summaryError = ref<string | ProblemDetail | null>(null);
  const syncingIds = ref(new Set<string>());
  const unsubHandlers: (() => void)[] = [];

  const totalSpend = computed(() => {
    if (!Array.isArray(summary.value?.trend)) return 0;
    return summary.value.trend.reduce((sum, d) => sum + (d.token_spend_usd || 0), 0);
  });

  async function fetchSummary() {
    if (loading.value) return;
    loading.value = true;
    summaryError.value = null;
    try {
      const { data: result, error: err } = await withTimeout(
        api.GET("/api/v1/dashboard/summary"),
        15000,
        "Dashboard summary request",
      );
      if (err) {
        summaryError.value = formatApiError(err);
      } else {
        summary.value = validateDashboardSummary(result);
        if (!summary.value) summaryError.value = t("views.DashboardView.invalid_dashboard_data");
      }
    } catch (e: unknown) {
      summaryError.value = formatApiError(e);
    } finally {
      loading.value = false;
    }
  }

  const trends = ref<TrendsResponse | null>(null);
  const trendsLoading = ref(false);
  const trendsError = ref<string | ProblemDetail | null>(null);
  const error = computed(() => summaryError.value || trendsError.value);

  const TRENDS_REQUIRED_KEYS = [
    "run_counts", "eval_pass_rates", "token_spend",
    "hitl_volume", "rejection_trend", "correlation", "feedback_volume",
  ] as const;

  function validateTrendsResponse(data: unknown): data is TrendsResponse {
    if (!data || typeof data !== "object") return false;
    const d = data as Record<string, unknown>;
    for (const key of TRENDS_REQUIRED_KEYS) {
      if (!Array.isArray(d[key])) return false;
    }
    return true;
  }

  async function fetchTrends(days: number) {
    if (trendsLoading.value) return;
    if (!Number.isInteger(days) || days <= 0) {
      trendsError.value = t("views.DashboardView.invalid_days_parameter");
      return;
    }
    trendsLoading.value = true;
    trendsError.value = null;
    try {
      const { data: result, error: err } = await withTimeout(
        api.GET("/api/v1/dashboard/trends", {
          params: { query: { days } },
        } as any),
        15000,
        "Dashboard trends request",
      );
      if (err) {
        trendsError.value = formatApiError(err);
      } else if (validateTrendsResponse(result)) {
        trends.value = result;
      } else {
        trendsError.value = t("views.DashboardView.invalid_trends_data");
      }
    } catch (e: unknown) {
      trendsError.value = formatApiError(e);
    } finally {
      trendsLoading.value = false;
    }
  }

  function handleSyncEvent(event: EventBusEvent): void {
    if (event.type !== "run" && event.type !== "pipeline") return;
    if (!syncingIds.value.has(event.id)) {
      syncingIds.value.add(event.id);
      void fetchSummary().finally(() => {
        syncingIds.value.delete(event.id);
      });
    }
  }

  unsubHandlers.push(registerHandler("run", handleSyncEvent));
  unsubHandlers.push(registerHandler("pipeline", handleSyncEvent));

  if (import.meta.hot) {
    import.meta.hot.dispose(() => {
      disposeHandlers();
    });
  }

  function disposeHandlers(): void {
    for (const unsub of unsubHandlers) unsub();
    unsubHandlers.length = 0;
    syncingIds.value.clear();
  }

  return {
    summary,
    trends,
    loading,
    trendsLoading,
    error,
    summaryError,
    trendsError,
    totalSpend,
    fetchSummary,
    fetchTrends,
    disposeHandlers,
  };
});
