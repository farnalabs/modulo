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
      // The backend route is GET /api/v1/composite-templates (composite_templates.py),
      // and it is paginated (page/page_size, max 100). Walk every page so the
      // id→template map is complete — a silent first-page-only load would make
      // getCompositeById miss templates past page_size.
      const all: CompositeDefinition[] = [];
      let page = 1;
      for (;;) {
        const result = await withTimeout(
          get<{ items: CompositeDefinition[]; total?: number }>(
            `/api/v1/composite-templates?page=${page}&page_size=100`,
          ),
          15000,
          "Composites request",
        );
        const items = result.items || [];
        all.push(...items);
        page += 1;
        // Stop when the page was empty, the API reported no more rows than we
        // hold, or a payload without `total` (single-page) was returned.
        const total = typeof result.total === "number" ? result.total : all.length;
        if (items.length === 0 || all.length >= total) break;
      }
      composites.value = all;
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
