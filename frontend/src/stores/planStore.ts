import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { api } from "../lib/api/client";
import { withTimeout } from "../lib/asyncUtils";
import { formatApiError } from "../lib/api/formatError";
import { registerHandler } from "./syncRegistry";
import type { EventBusEvent } from "@/types/events";

interface ApiResult<T> {
  data?: T;
  error?: unknown;
}

interface FeatureFlagsPayload {
  license: { tier: string };
  dev_mode?: boolean;
  flags: Array<{ name: string; currently_active: boolean }>;
}

interface LicensePayload {
  expires_at?: string | null;
  org_id?: string | null;
  tier?: string;
}

interface TiersPayload {
  tiers: Array<{ tier_id: string; label: string; rank: number }>;
}

function pushSettledError(apiErrors: string[], label: string, reason: unknown): void {
  const message = (reason as { message?: unknown } | null | undefined)?.message;
  apiErrors.push(`${label}: ${message ?? String(reason)}`);
}

function runPlanSource<T>(
  label: string,
  settled: PromiseSettledResult<unknown>,
  apply: (res: ApiResult<T>, apiErrors: string[]) => void,
  apiErrors: string[],
): void {
  if (settled.status === "fulfilled") {
    apply(settled.value as unknown as ApiResult<T>, apiErrors);
  } else {
    pushSettledError(apiErrors, label, settled.reason);
  }
}

export const usePlanStore = defineStore("plan", () => {
  const currentTier = ref("community");
  const features = ref<Record<string, boolean>>({});
  const devMode = ref(false);
  const isLoading = ref(false);
  const loaded = ref(false);
  const error = ref<string | null>(null);
  const expiresAt = ref<string | null>(null);
  const orgId = ref<string | null>(null);
  const tierLabels = ref<Record<string, string>>({});
  const tierRanks = ref<Record<string, number>>({
    community: 0,
    team: 1,
  });
  const syncingIds = ref(new Set<string>());
  const unsubHandlers: (() => void)[] = [];

  const isTeam = computed(() => isAtMinimumTier("team"));

  function featureEnabled(name: string): boolean {
    const override = orgOverrides.value[name];
    if (override !== undefined && override !== null) return override;
    return features.value[name] ?? false;
  }

  function getTierLabel(tierId: string): string {
    return (
      tierLabels.value[tierId] ??
      tierId.charAt(0).toUpperCase() + tierId.slice(1)
    );
  }

  function isAtMinimumTier(minTier: string): boolean {
    const currentRank = tierRanks.value[currentTier.value];
    const minRank = tierRanks.value[minTier];
    if (currentRank === undefined || minRank === undefined) return false;
    return currentRank >= minRank;
  }

  let fetchPlanPromise: Promise<void> | null = null;

  function applyFeatureFlagsPayload(res: ApiResult<FeatureFlagsPayload>, apiErrors: string[]): void {
    if (res.error) {
      apiErrors.push(`Feature flags: ${formatApiError(res.error)}`);
    } else if (res.data) {
      currentTier.value = res.data.license.tier;
      devMode.value = res.data.dev_mode === true;
      const map: Record<string, boolean> = {};
      for (const flag of res.data.flags) {
        map[flag.name] = flag.currently_active;
      }
      features.value = map;
      loaded.value = true;
    }
  }

  function applyLicensePayload(res: ApiResult<LicensePayload>, apiErrors: string[]): void {
    if (res.error) {
      apiErrors.push(`License: ${formatApiError(res.error)}`);
    } else if (res.data) {
      expiresAt.value = res.data.expires_at ?? null;
      orgId.value = res.data.org_id ?? null;
      if (res.data.tier) currentTier.value = res.data.tier;
    }
  }

  function applyTiersPayload(res: ApiResult<TiersPayload>, apiErrors: string[]): void {
    if (res.error) {
      apiErrors.push(`Tiers: ${formatApiError(res.error)}`);
    } else if (res.data?.tiers?.length) {
      const labels: Record<string, string> = {};
      const ranks: Record<string, number> = {};
      for (const t of res.data.tiers) {
        labels[t.tier_id] = t.label;
        ranks[t.tier_id] = t.rank;
      }
      tierLabels.value = labels;
      tierRanks.value = ranks;
    }
  }

  async function fetchPlan() {
    if (fetchPlanPromise) return fetchPlanPromise;
    fetchPlanPromise = doFetchPlan();
    try {
      await fetchPlanPromise;
    } finally {
      fetchPlanPromise = null;
    }
  }

  async function doFetchPlan() {
    if (isLoading.value) return;
    isLoading.value = true;
    error.value = null;
    const apiErrors: string[] = [];
    try {
      const results = await Promise.allSettled([
        withTimeout(
          api.GET("/api/v1/admin/feature-flags"),
          15000,
          "Feature flags request",
        ),
        withTimeout(
          api.GET("/api/v1/admin/license"),
          15000,
          "License request",
        ),
        withTimeout(
          api.GET("/api/v1/admin/tiers"),
          15000,
          "Tiers request",
        ),
      ]);

      const [flagsSettled, licenseSettled, tiersSettled] = results;

      runPlanSource("Feature flags", flagsSettled, applyFeatureFlagsPayload, apiErrors);
      runPlanSource("License", licenseSettled, applyLicensePayload, apiErrors);
      runPlanSource("Tiers", tiersSettled, applyTiersPayload, apiErrors);

      error.value = apiErrors.length > 0 ? apiErrors.join("; ") : null;
    } catch (e: unknown) {
      error.value = formatApiError(e);
    } finally {
      isLoading.value = false;
    }
  }

  function handleSyncEvent(event: EventBusEvent): void {
    if (
      event.type === "team" ||
      event.type === "license" ||
      event.type === "plan"
    ) {
      if (!syncingIds.value.has(event.id)) {
        syncingIds.value.add(event.id);
        void fetchPlan().finally(() => {
          syncingIds.value.delete(event.id);
        });
      }
    }
  }

  unsubHandlers.push(registerHandler("team", handleSyncEvent));
  unsubHandlers.push(registerHandler("license", handleSyncEvent));
  unsubHandlers.push(registerHandler("plan", handleSyncEvent));

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

  const orgOverrides = ref<Record<string, boolean | null>>({});

  async function fetchOrgFlagOverride(flagName: string): Promise<boolean | null> {
    try {
      const res = await api.GET(
        '/api/v1/admin/feature-flags/{flag_name}/org-override',
        { params: { path: { flag_name: flagName } } },
      );
      if (res.error) return null;
      const data = res.data as { override: boolean | null } | undefined;
      return data?.override ?? null;
    } catch (err) {
      console.warn('[plan] Failed to fetch org flag override', err);
      return null;
    }
  }

  async function setOrgFlagOverride(flagName: string, enabled: boolean | null): Promise<boolean> {
    try {
      if (enabled === null) {
        const res = await api.DELETE(
          '/api/v1/admin/feature-flags/{flag_name}/org-override',
          { params: { path: { flag_name: flagName } } },
        );
        if (res.error) return false;
      } else {
        const res = await api.PUT(
          '/api/v1/admin/feature-flags/{flag_name}/org-override',
          {
            params: { path: { flag_name: flagName } },
            body: { enabled },
          },
        );
        if (res.error) return false;
      }
      orgOverrides.value[flagName] = enabled;
      return true;
    } catch (err) {
      console.warn('[plan] Failed to set org flag override', err);
      return false;
    }
  }

  return {
    currentTier,
    features,
    devMode,
    isLoading,
    loaded,
    error,
    isTeam,
    expiresAt,
    orgId,
    tierLabels,
    tierRanks,
    orgOverrides,
    fetchPlan,
    featureEnabled,
    getTierLabel,
    isAtMinimumTier,
    fetchOrgFlagOverride,
    setOrgFlagOverride,
    disposeHandlers,
  };
});
