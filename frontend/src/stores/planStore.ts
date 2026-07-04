import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { api } from "../lib/api/client";
import { formatApiError } from "../lib/api/formatError";
import { registerHandler } from "./syncRegistry";
import type { EventBusEvent } from "@/types/events";

export const usePlanStore = defineStore("plan", () => {
  const currentTier = ref("community");
  const features = ref<Record<string, boolean>>({});
  const isLoading = ref(false);
  const error = ref<string | null>(null);
  const expiresAt = ref<string | null>(null);
  const orgName = ref<string | null>(null);
  const tierLabels = ref<Record<string, string>>({});
  const tierRanks = ref<Record<string, number>>({});
  const syncingIds = ref(new Set<string>());
  const unsubHandlers: (() => void)[] = [];
  let hasLoadedOnce = false;

  const isTeam = computed(() => isAtMinimumTier("team"));

  function featureEnabled(name: string): boolean {
    if (Object.keys(features.value).length === 0) {
      if (hasLoadedOnce) return true;
      return false;
    }
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

  async function fetchPlan() {
    if (isLoading.value) return;
    isLoading.value = true;
    error.value = null;
    const apiErrors: string[] = [];
    try {
      const results = await Promise.allSettled([
        Promise.race([
          api.GET("/api/v1/admin/feature-flags"),
          new Promise<never>((_, reject) =>
            setTimeout(() => reject(new Error("Feature flags request timed out after 15s")), 15000),
          ),
        ]),
        Promise.race([
          api.GET("/api/v1/admin/license"),
          new Promise<never>((_, reject) =>
            setTimeout(() => reject(new Error("License request timed out after 15s")), 15000),
          ),
        ]),
        Promise.race([
          api.GET("/api/v1/admin/tiers"),
          new Promise<never>((_, reject) =>
            setTimeout(() => reject(new Error("Tiers request timed out after 15s")), 15000),
          ),
        ]),
      ]);

      const [flagsSettled, licenseSettled, tiersSettled] = results;

      if (flagsSettled.status === "fulfilled") {
        const flagsRes = flagsSettled.value;
        if (flagsRes.error) {
          apiErrors.push(`Feature flags: ${formatApiError(flagsRes.error)}`);
        } else if (flagsRes.data) {
          currentTier.value = flagsRes.data.license.tier;
          const map: Record<string, boolean> = {};
          for (const flag of flagsRes.data.flags) {
            map[flag.name] = flag.currently_active;
          }
          features.value = map;
        }
      } else {
        apiErrors.push(`Feature flags: ${flagsSettled.reason?.message ?? String(flagsSettled.reason)}`);
      }

      if (licenseSettled.status === "fulfilled") {
        const licenseRes = licenseSettled.value;
        if (licenseRes.error) {
          apiErrors.push(`License: ${formatApiError(licenseRes.error)}`);
        } else if (licenseRes.data) {
          expiresAt.value = licenseRes.data.expires_at ?? null;
          orgName.value = licenseRes.data.org_id ?? null;
          if (licenseRes.data.tier) currentTier.value = licenseRes.data.tier;
        }
      } else {
        apiErrors.push(`License: ${licenseSettled.reason?.message ?? String(licenseSettled.reason)}`);
      }

      if (tiersSettled.status === "fulfilled") {
        const tiersRes = tiersSettled.value;
        if (tiersRes.error) {
          apiErrors.push(`Tiers: ${formatApiError(tiersRes.error)}`);
        } else if (tiersRes.data) {
          const labels: Record<string, string> = {};
          const ranks: Record<string, number> = {};
          for (const t of tiersRes.data.tiers) {
            labels[t.tier_id] = t.label;
            ranks[t.tier_id] = t.rank;
          }
          tierLabels.value = labels;
          tierRanks.value = ranks;
        }
      } else {
        apiErrors.push(`Tiers: ${tiersSettled.reason?.message ?? String(tiersSettled.reason)}`);
      }

      error.value = apiErrors.length > 0 ? apiErrors.join("; ") : null;
      if (!error.value) hasLoadedOnce = true;
    } catch (e: unknown) {
      apiErrors.push(e instanceof Error ? e.message : String(e));
      error.value = apiErrors.join("; ");
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

  return {
    currentTier,
    features,
    isLoading,
    error,
    isTeam,
    expiresAt,
    orgName,
    tierLabels,
    tierRanks,
    fetchPlan,
    featureEnabled,
    getTierLabel,
    isAtMinimumTier,
    disposeHandlers,
  };
});
