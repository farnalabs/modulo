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

  const isTeam = computed(() => currentTier.value === "team");

  function featureEnabled(name: string): boolean {
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
      const [flagsRes, licenseRes, tiersRes] = await Promise.all([
        api.GET("/api/v1/admin/feature-flags"),
        api.GET("/api/v1/admin/license"),
        api.GET("/api/v1/admin/tiers"),
      ]);

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

      if (licenseRes.error) {
        apiErrors.push(`License: ${formatApiError(licenseRes.error)}`);
      } else if (licenseRes.data) {
        expiresAt.value = licenseRes.data.expires_at ?? null;
        orgName.value = licenseRes.data.org_id ?? null;
        if (licenseRes.data.tier) currentTier.value = licenseRes.data.tier;
      }

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

      error.value = apiErrors.length > 0 ? apiErrors.join("; ") : null;
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
