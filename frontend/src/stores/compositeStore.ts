import { defineStore } from "pinia";
import { ref, computed } from "vue";
import { useApi } from "../composables/useApi";
import { withTimeout, asErrorMessage } from "../lib/asyncUtils";
import type { CompositeDefinition } from "../types/pipeline";

export const useCompositeStore = defineStore("composite", () => {
  const composites = ref<CompositeDefinition[]>([]);
  const loading = ref(false);
  const error = ref<string | null>(null);
  let hasLoadedOnce = false;

  const { get } = useApi();

  const compositeMap = computed(() => {
    const map = new Map<string, CompositeDefinition>();
    for (const c of composites.value) {
      map.set(c.id, c);
    }
    return map;
  });

  async function loadComposites() {
    if (loading.value) return;
    if (hasLoadedOnce) return;
    loading.value = true;
    error.value = null;
    try {
      const result = await withTimeout(
        get<{ items: CompositeDefinition[] }>("/api/v1/composites"),
        15000,
        "Composites request",
      );
      composites.value = result.items || [];
      hasLoadedOnce = true;
    } catch (e: unknown) {
      error.value = asErrorMessage(e);
      if (!hasLoadedOnce) composites.value = [];
    } finally {
      loading.value = false;
    }
  }

  function getCompositeById(id: string): CompositeDefinition | undefined {
    return compositeMap.value.get(id);
  }

  function disposeHandlers() {
    composites.value = [];
    error.value = null;
    hasLoadedOnce = false;
  }

  if (import.meta.hot) {
    import.meta.hot.dispose(() => {
      disposeHandlers();
    });
  }

  return {
    composites,
    loading,
    error,
    loadComposites,
    getCompositeById,
    disposeHandlers,
  };
});
