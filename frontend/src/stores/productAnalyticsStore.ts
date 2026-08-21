import { defineStore } from "pinia";
import { ref } from "vue";
import { api } from "../lib/api/client";

interface TransparencyData {
  last_successful_dump_at: string | null;
  dump_count_total: number;
  dump_count_last_7d: number;
  consent_level: string;
  instance_enabled: boolean;
  enforcement_enabled: boolean;
  warning: string | null;
}

export const useProductAnalyticsStore = defineStore("productAnalytics", () => {
  const transparency = ref<TransparencyData | null>(null);
  const isLoading = ref(false);
  const error = ref<string | null>(null);

  async function fetchTransparency() {
    isLoading.value = true;
    error.value = null;
    try {
      const { data, error: apiError } = await api.GET(
        "/api/v1/product-analytics/transparency",
      );
      if (apiError) {
        error.value =
          typeof apiError === "string"
            ? apiError
            : "Failed to load transparency data";
        return;
      }
      transparency.value = data as TransparencyData;
    } catch (err: unknown) {
      error.value =
        err instanceof Error ? err.message : "Failed to load transparency data";
    } finally {
      isLoading.value = false;
    }
  }

  return {
    transparency,
    isLoading,
    error,
    fetchTransparency,
  };
});
