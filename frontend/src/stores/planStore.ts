import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { api, getAccessToken } from "../lib/api/client";
import { withTimeout } from "../lib/asyncUtils";
import { formatApiError } from "../lib/api/formatError";
import { registerSyncHandlers, disposeSyncHandlers } from "./syncRegistry";
import { flagCacheKey, parseFlagCache, serializeFlagCache } from "../config/flagCache";
import { decodeJwtPayload } from "../lib/jwt";
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

/**
 * The org the current access token belongs to, read synchronously from the
 * JWT `org_id` claim. Being synchronous matters: the flag cache must be
 * scoped per org BEFORE first paint, when no server payload has landed yet
 * (FAR-1237 review finding: the cache was per-origin, so signing into org B
 * first-painted org A's resolved chrome). Returns null when the token is
 * absent/opaque — those sessions share the `unknown` bucket.
 */
function currentOrgId(): string | null {
  try {
    const payload = decodeJwtPayload(getAccessToken());
    const orgId = payload?.org_id;
    return typeof orgId === "string" && orgId.length > 0 ? orgId : null;
  } catch {
    // No storage / no token helper available: fall back to the unknown bucket.
    return null;
  }
}

/**
 * Synchronous read of the persisted flag map for `orgId` (FAR-1237). Runs at
 * store creation — before first paint — so flag-driven decisions (mobile nav
 * layout, gated surfaces) start from the LAST RESOLVED value for THIS org
 * instead of an empty "everything off" map that later flips when the request
 * lands.
 */
function readFlagCache(orgId: string | null): Record<string, boolean> | null {
  try {
    if (typeof localStorage === "undefined") return null;
    return parseFlagCache(localStorage.getItem(flagCacheKey(orgId)));
  } catch {
    // Storage blocked (private mode, disabled cookies): behave as uncached.
    return null;
  }
}

/** Best-effort persist — quota/private-mode failures must not break the fetch path. */
function writeFlagCache(flags: Record<string, boolean>): void {
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.setItem(flagCacheKey(currentOrgId()), serializeFlagCache(flags));
  } catch (err) {
    // Best-effort only: the in-memory map is already correct without it.
    console.warn("[plan] Failed to persist flag cache", err);
  }
}

export const usePlanStore = defineStore("plan", () => {
  const cachedFlags = readFlagCache(currentOrgId());
  const currentTier = ref("community");
  const features = ref<Record<string, boolean>>(cachedFlags ?? {});
  // Where the current flag map came from. 'none' = nothing has ever resolved,
  // the ONLY state in which a flag value is unknown (layout consumers render
  // their 'pending' placeholder then). Write-once: neither a cache hit nor a
  // server payload ever returns it to 'none', so a resolved layout can never
  // fall back to the unresolved state mid-session (FAR-1237 guard). Note
  // `loaded` stays false on a cache hit — it means "fetched from the server",
  // which the router's tier guard relies on.
  const flagsSource = ref<"none" | "cache" | "server">(
    cachedFlags ? "cache" : "none",
  );
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
      flagsSource.value = "server";
      loaded.value = true;
      writeFlagCache(map);
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

  registerSyncHandlers(unsubHandlers, syncingIds, ["team", "license", "plan"], handleSyncEvent);

  function disposeHandlers(): void {
    disposeSyncHandlers(unsubHandlers, syncingIds);
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
    flagsSource,
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
