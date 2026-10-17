<template>
  <section class="card p-4" data-testid="capacity-queue-panel" aria-labelledby="capacity-queue-heading">
    <div class="mb-3 flex items-start justify-between gap-3">
      <div>
        <h2 id="capacity-queue-heading" class="text-base font-semibold">
          {{ $t('views.AnalyticsView.capacity_panel_title') }}
        </h2>
        <p class="mt-0.5 text-xs text-muted-foreground">
          {{ $t('views.AnalyticsView.capacity_panel_subtitle') }}
        </p>
      </div>
      <div class="flex items-center gap-2">
        <output
          v-if="store.capacity"
          class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium"
          :class="verdictClass"
          data-testid="capacity-verdict"
          aria-labelledby="capacity-queue-heading"
        >
          {{ verdictLabel }}
        </output>
        <button
          type="button"
          class="rounded-md border border-border px-2.5 py-1 text-xs font-medium transition-colors hover:bg-muted"
          data-testid="capacity-refresh"
          :disabled="store.capacityLoading"
          @click="store.fetchCapacity()"
        >
          {{ $t('views.AnalyticsView.capacity_refresh') }}
        </button>
      </div>
    </div>

    <p
      v-if="store.capacityError"
      class="rounded-md border border-destructive/40 bg-destructive/10 p-2.5 text-sm text-destructive"
      role="alert"
      data-testid="capacity-error"
    >
      {{ store.capacityError }}
    </p>

    <div v-else-if="store.capacityLoading && !store.capacity" class="text-sm text-muted-foreground" data-testid="capacity-loading">
      {{ $t('views.AnalyticsView.capacity_loading') }}
    </div>

    <div v-else-if="store.capacity" class="space-y-4">
      <div
        v-if="health.stall_suspected"
        class="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive"
        role="alert"
        data-testid="capacity-stall-alert"
      >
        <span class="mt-1.5 inline-block h-2 w-2 shrink-0 rounded-full bg-destructive" aria-hidden="true"></span>
        <div>
          <p class="font-medium">{{ $t('views.AnalyticsView.capacity_stall_title') }}</p>
          <p class="mt-0.5">{{ $t('views.AnalyticsView.capacity_stall_detail', { intervals: health.stall_intervals }) }}</p>
        </div>
      </div>

      <dl class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div class="rounded-lg border border-border p-3">
          <dt class="text-xs text-muted-foreground">{{ $t('views.AnalyticsView.capacity_pending_now') }}</dt>
          <dd class="mt-1 text-lg font-semibold tabular-nums" data-testid="capacity-pending-total">{{ capacity.pending.total }}</dd>
        </div>
        <div class="rounded-lg border border-border p-3">
          <dt class="text-xs text-muted-foreground">{{ $t('views.AnalyticsView.capacity_oldest_pending') }}</dt>
          <dd class="mt-1 text-lg font-semibold tabular-nums" data-testid="capacity-oldest-pending">
            {{ formatDuration(capacity.pending.oldest_pending_age_seconds) }}
          </dd>
        </div>
        <div class="rounded-lg border border-border p-3">
          <dt class="text-xs text-muted-foreground">{{ $t('views.AnalyticsView.capacity_drain_ratio') }}</dt>
          <dd class="mt-1 text-lg font-semibold tabular-nums" data-testid="capacity-drain-ratio">{{ drainRatioLabel }}</dd>
        </div>
        <div class="rounded-lg border border-border p-3">
          <dt class="text-xs text-muted-foreground">{{ $t('views.AnalyticsView.capacity_org_cap_use') }}</dt>
          <dd class="mt-1 text-lg font-semibold tabular-nums" data-testid="capacity-org-cap-use">{{ orgCapUseLabel }}</dd>
        </div>
      </dl>

      <div class="rounded-lg border border-border p-3 text-sm" data-testid="capacity-limits">
        <div class="flex flex-wrap gap-x-6 gap-y-1">
          <span>
            {{ $t('views.AnalyticsView.capacity_org_cap') }}:
            <span class="font-medium tabular-nums">{{ capacity.limits.org_run_concurrency_limit ?? $t('views.AnalyticsView.capacity_none') }}</span>
          </span>
          <span>
            {{ $t('views.AnalyticsView.capacity_sandbox_cap') }}:
            <span class="font-medium tabular-nums">{{ sandboxCapLabel }}</span>
          </span>
        </div>
        <div class="mt-2 flex flex-wrap gap-x-6 gap-y-1" data-testid="capacity-binding">
          <span>
            {{ $t('views.AnalyticsView.capacity_binding') }}:
            <span class="font-medium">{{ bindingLabel }}</span>
          </span>
          <span v-if="capacity.binding.headroom != null">
            {{ $t('views.AnalyticsView.capacity_headroom') }}:
            <span class="font-medium tabular-nums">{{ capacity.binding.headroom }}</span>
          </span>
          <span>
            {{ $t('views.AnalyticsView.capacity_if_raised') }}:
            <span class="font-medium tabular-nums">{{ capacity.queue_if_raised.admissible_if_binding_raised }}</span>
          </span>
        </div>
      </div>

      <div data-testid="capacity-causes">
        <h3 class="mb-2 text-sm font-medium">{{ $t('views.AnalyticsView.capacity_cause_title') }}</h3>
        <div class="flex flex-wrap gap-3 text-sm">
          <span v-for="cause in causes" :key="cause.key" class="rounded-md border border-border px-2.5 py-1">
            {{ $t(cause.labelKey) }}:
            <span class="font-medium tabular-nums" :data-testid="`capacity-cause-${cause.key}`">{{ cause.count }}</span>
          </span>
        </div>
      </div>

      <div data-testid="capacity-active-health">
        <h3 class="mb-2 text-sm font-medium">{{ $t('views.AnalyticsView.capacity_health_title') }}</h3>
        <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div class="rounded-lg border border-border p-3">
            <p class="text-xs text-muted-foreground">{{ $t('views.AnalyticsView.capacity_executing') }}</p>
            <p class="mt-1 font-semibold tabular-nums" data-testid="capacity-executing-total">{{ health.executing_total }}</p>
          </div>
          <div class="rounded-lg border border-border p-3">
            <p class="text-xs text-muted-foreground">{{ $t('views.AnalyticsView.capacity_parked') }}</p>
            <p class="mt-1 font-semibold tabular-nums" data-testid="capacity-parked-total">{{ health.parked_total }}</p>
          </div>
          <div class="rounded-lg border border-border p-3">
            <p class="text-xs text-muted-foreground">{{ $t('views.AnalyticsView.capacity_completions') }}</p>
            <p class="mt-1 font-semibold tabular-nums" data-testid="capacity-completions">{{ health.completions_in_window }}</p>
          </div>
          <div class="rounded-lg border border-border p-3">
            <p class="text-xs text-muted-foreground">{{ $t('views.AnalyticsView.capacity_zero_intervals') }}</p>
            <p class="mt-1 font-semibold tabular-nums" data-testid="capacity-zero-intervals">{{ health.zero_completion_intervals }}</p>
          </div>
        </div>
      </div>

      <div v-if="capacity.pending.by_pipeline.length" data-testid="capacity-by-pipeline">
        <h3 class="mb-2 text-sm font-medium">{{ $t('views.AnalyticsView.capacity_by_pipeline_title') }}</h3>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b text-left text-muted-foreground">
                <th class="pb-2 font-medium">{{ $t('views.AnalyticsView.capacity_col_pipeline') }}</th>
                <th class="pb-2 text-right font-medium">{{ $t('views.AnalyticsView.capacity_col_pending') }}</th>
                <th class="pb-2 text-right font-medium">{{ $t('views.AnalyticsView.capacity_col_oldest') }}</th>
                <th class="pb-2 text-right font-medium">{{ $t('views.AnalyticsView.capacity_col_cap') }}</th>
                <th class="pb-2 text-right font-medium">{{ $t('views.AnalyticsView.capacity_col_stale') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in pipelineRows" :key="row.pipeline_id" class="border-b last:border-0">
                <td class="py-2 font-medium">{{ row.pipeline_name || shortId(row.pipeline_id) }}</td>
                <td class="py-2 text-right tabular-nums">{{ row.pending_count }}</td>
                <td class="py-2 text-right tabular-nums">{{ formatDuration(row.oldest_pending_age_seconds) }}</td>
                <td class="py-2 text-right tabular-nums">{{ row.max_concurrent_runs ?? $t('views.AnalyticsView.capacity_none') }}</td>
                <td class="py-2 text-right tabular-nums">{{ row.stale_heartbeat_count }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="capacity.series.length" data-testid="capacity-series">
        <h3 class="mb-2 text-sm font-medium">{{ $t('views.AnalyticsView.capacity_series_title') }}</h3>
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b text-left text-muted-foreground">
                <th class="pb-2 font-medium">{{ $t('views.AnalyticsView.capacity_col_interval') }}</th>
                <th class="pb-2 text-right font-medium">{{ $t('views.AnalyticsView.capacity_col_created') }}</th>
                <th class="pb-2 text-right font-medium">{{ $t('views.AnalyticsView.capacity_col_started') }}</th>
                <th class="pb-2 text-right font-medium">{{ $t('views.AnalyticsView.capacity_col_completed') }}</th>
                <th class="pb-2 text-right font-medium">{{ $t('views.AnalyticsView.capacity_col_depth') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="bucket in capacity.series" :key="bucket.interval_start" class="border-b last:border-0">
                <td class="py-2 tabular-nums">{{ formatInterval(bucket.interval_start) }}</td>
                <td class="py-2 text-right tabular-nums">{{ bucket.created }}</td>
                <td class="py-2 text-right tabular-nums">{{ bucket.started }}</td>
                <td class="py-2 text-right tabular-nums">{{ bucket.completed }}</td>
                <td class="py-2 text-right tabular-nums">{{ bucket.pending_depth }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <p class="text-xs text-muted-foreground">
        {{ $t('views.AnalyticsView.capacity_generated_at', { at: formatInterval(capacity.generated_at) }) }}
      </p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  formatCapacityDuration,
  useAnalyticsStore,
  type CapacityActiveHealth,
  type CapacityAttribution,
  type CapacityResponse,
} from "../../stores/analytics";

const { t } = useI18n();
const store = useAnalyticsStore();

// A zeroed stand-in keeps the template free of null-guards: the panel renders
// its body only when `store.capacity` is present (v-else-if below).
const EMPTY_CAPACITY: CapacityResponse = {
  generated_at: "",
  window_start: "",
  window_seconds: 0,
  interval_seconds: 0,
  limits: { org_run_concurrency_limit: null, sandbox_concurrency_limit: null, sandbox_limit_is_default: false },
  pending: { total: 0, oldest_pending_age_seconds: null, by_cause: {}, by_pipeline: [] },
  drain: {
    created: 0,
    started: 0,
    completed: 0,
    ratio: null,
    baseline_depth: 0,
    baseline_oldest_pending_age_seconds: null,
    final_depth: 0,
    depth_change: 0,
    verdict: "stable",
  },
  binding: { scope: "none", limit: null, in_use: null, headroom: null, pending_blocked: 0, admissible_if_raised: 0 },
  queue_if_raised: { pending_now: 0, admissible_if_binding_raised: 0, pending_after_raise: 0 },
  active_health: {
    org_run_limit: null,
    gate_total: 0,
    executing_total: 0,
    running_total: 0,
    parked_total: 0,
    cancelling_total: 0,
    utilisation: null,
    cap_held: false,
    completions_in_window: 0,
    zero_completion_intervals: 0,
    stall_intervals: 3,
    stall_suspected: false,
    stale_heartbeat_threshold_seconds: 1800,
    stale_heartbeat_count: 0,
    attribution: [],
  },
  series: [],
};

const capacity = computed<CapacityResponse>(() => store.capacity ?? EMPTY_CAPACITY);

const health = computed<CapacityActiveHealth>(() => capacity.value.active_health);

function formatDuration(seconds: number | null | undefined): string {
  return formatCapacityDuration(seconds);
}

function formatInterval(iso: string): string {
  const date = new Date(iso); // nosemgrep: new-date-without-guard
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

const VERDICT_KEYS: Record<string, string> = {
  draining: "views.AnalyticsView.capacity_verdict_draining",
  stable: "views.AnalyticsView.capacity_verdict_stable",
  backing_up: "views.AnalyticsView.capacity_verdict_backing_up",
  stalled: "views.AnalyticsView.capacity_verdict_stalled",
};

const verdictLabel = computed(() => {
  const verdict = capacity.value.drain.verdict ?? "stable";
  return t(VERDICT_KEYS[verdict] ?? "views.AnalyticsView.capacity_verdict_stable");
});

const verdictClass = computed(() => {
  const verdict = capacity.value.drain.verdict;
  if (verdict === "stalled" || verdict === "backing_up") {
    return "bg-destructive/10 text-destructive";
  }
  if (verdict === "draining") {
    return "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
  }
  return "bg-muted text-muted-foreground";
});

const drainRatioLabel = computed(() => {
  const ratio = capacity.value.drain.ratio;
  return ratio == null ? "—" : ratio.toFixed(2);
});

const orgCapUseLabel = computed(() => {
  const utilisation = health.value.utilisation;
  if (utilisation == null) return "—";
  return `${Math.round(utilisation * 100)}%`;
});

const sandboxCapLabel = computed(() => {
  const cap = capacity.value.limits.sandbox_concurrency_limit;
  if (cap == null) return t("views.AnalyticsView.capacity_none");
  return capacity.value.limits.sandbox_limit_is_default
    ? `${cap} (${t("views.AnalyticsView.capacity_default")})`
    : String(cap);
});

const BINDING_KEYS: Record<string, string> = {
  org_run: "views.AnalyticsView.capacity_binding_org_run",
  sandbox: "views.AnalyticsView.capacity_binding_sandbox",
  pipeline: "views.AnalyticsView.capacity_binding_pipeline",
};

const bindingLabel = computed(() => {
  const scope = capacity.value.binding.scope;
  if (!scope || scope === "none") return t("views.AnalyticsView.capacity_binding_none");
  return t(BINDING_KEYS[scope] ?? "views.AnalyticsView.capacity_binding_none");
});

const CAUSES = [
  { key: "pipeline", labelKey: "views.AnalyticsView.capacity_cause_pipeline" },
  { key: "org_run", labelKey: "views.AnalyticsView.capacity_cause_org_run" },
  { key: "sandbox", labelKey: "views.AnalyticsView.capacity_cause_sandbox" },
  { key: "unmarked", labelKey: "views.AnalyticsView.capacity_cause_unmarked" },
];

const causes = computed(() =>
  CAUSES.map((cause) => ({
    ...cause,
    count: capacity.value.pending.by_cause[cause.key] ?? 0,
  })),
);

interface PipelineRow {
  pipeline_id: string;
  pipeline_name?: string | null;
  pending_count: number;
  oldest_pending_age_seconds?: number | null;
  max_concurrent_runs?: number | null;
  stale_heartbeat_count: number;
}

const pipelineRows = computed<PipelineRow[]>(() => {
  const attribution = new Map<string, CapacityAttribution>(
    capacity.value.active_health.attribution.map((entry) => [entry.pipeline_id, entry]),
  );
  return capacity.value.pending.by_pipeline.map((row) => ({
    ...row,
    stale_heartbeat_count: attribution.get(row.pipeline_id)?.stale_heartbeat_count ?? 0,
  }));
});

onMounted(() => {
  void store.fetchCapacity();
});

// The panel is part of the analytics surface: when the operator narrows to a
// pipeline the capacity scope follows. The window knobs stay at their defaults
// — a live queue is a "now" surface, not the facts series' date range.
watch(
  () => store.filters.pipelineId,
  () => {
    void store.fetchCapacity();
  },
);
</script>
