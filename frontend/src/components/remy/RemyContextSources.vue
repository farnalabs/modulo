<template>
  <div class="remy-cs flex flex-col flex-1 overflow-hidden">
    <div class="p-3 border-b">
      <h3 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {{ $t('components.remy.RemyContextSources.knowledge_sources') }}
      </h3>
      <p class="text-xs text-muted-foreground mt-1">
        {{ $t('components.remy.RemyContextSources.description') }}
      </p>
    </div>

    <div v-if="error" class="px-3 py-2 text-sm text-destructive">
      {{ error }}
    </div>

    <div class="flex-1 overflow-auto">
      <div v-if="loading" class="flex items-center justify-center py-12">
        <p class="text-sm text-muted-foreground">{{ $t('common.loading') }}</p>
      </div>
      <template v-else>
        <div class="remy-cs-header grid grid-cols-[1.5fr_auto_auto] gap-3 px-3 py-2 text-xs font-medium text-muted-foreground border-b items-center">
          <span>{{ $t('components.remy.RemyContextSources.source') }}</span>
          <span>{{ $t('components.remy.RemyContextSources.mode') }}</span>
          <span></span>
        </div>
        <div
          v-for="source in sources"
          :key="source.key"
          class="remy-cs-row grid grid-cols-[1.5fr_auto_auto] gap-3 px-3 py-2 text-sm items-center border-b"
        >
          <div class="min-w-0">
            <p class="text-sm font-medium truncate" :title="source.name">
              {{ source.name }}
              <span
                class="inline-flex items-center ml-1 align-middle cursor-help"
                :title="`Key: ${source.key}`"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-muted-foreground/50">
                  <circle cx="12" cy="12" r="10"/>
                  <path d="M12 16v-4"/>
                  <path d="M12 8h.01"/>
                </svg>
              </span>
            </p>
            <p class="text-xs text-muted-foreground truncate" :title="source.description || ''">
              {{ source.description || '—' }}
            </p>
          </div>
          <select
            class="remy-cs-select"
            aria-label="Knowledge source mode"
            :value="source.source_mode"
            :disabled="savingKey === source.key"
            @change="updateSource(source.key, ($event.target as HTMLSelectElement).value as ContextSourceMode)"
          >
            <option value="always_on">{{ $t('components.remy.RemyContextSources.always_on') }}</option>
            <option value="tool">{{ $t('components.remy.RemyContextSources.tool') }}</option>
            <option value="off">{{ $t('components.remy.RemyContextSources.off') }}</option>
          </select>
          <span
            v-if="!source.is_overridden"
            class="text-xs text-muted-foreground italic"
          >
            {{ $t('components.remy.RemyContextSources.org_default') }}
          </span>
          <span
            v-else
            class="text-xs text-primary italic"
          >
            {{ $t('components.remy.RemyContextSources.overridden') }}
          </span>
        </div>
      </template>
    </div>

    <div class="p-3 border-t flex items-center justify-between gap-2 flex-wrap">
      <Button severity="secondary" outlined size="small" class="text-xs" :disabled="resetting" @click="resetToDefaults">
        {{ $t('components.remy.RemyContextSources.reset_to_defaults') }}
      </Button>
      <div class="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <span>{{ $t('components.remy.RemyContextSources.legend_always_on') }}</span>
        <span>{{ $t('components.remy.RemyContextSources.legend_tool') }}</span>
        <span>{{ $t('components.remy.RemyContextSources.legend_off') }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api } from "@/lib/api/client";
import { formatApiError } from "@/lib/api/formatError";
import Button from 'primevue/button'
import type { ContextSourceItem, ContextSourceMode, ContextSourceUpdate } from "@/types/remy";

const sources = ref<ContextSourceItem[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);
const savingKey = ref<string | null>(null);
const resetting = ref(false);

async function fetchSources() {
  loading.value = true;
  error.value = null;
  try {
    const { data, error: err } = await api.GET("/api/v1/me/remy/context-sources");
    if (err) {
      error.value = `Failed to load sources: ${formatApiError(err)}`;
    } else if (data) {
      sources.value = (data as ContextSourceItem[]) ?? [];
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : "Failed to load sources";
  } finally {
    loading.value = false;
  }
}

async function updateSource(key: string, mode: ContextSourceMode) {
  savingKey.value = key;
  error.value = null;
  const prev = sources.value.find((s) => s.key === key);
  const prevMode = prev?.source_mode;
  if (prev) {
    prev.source_mode = mode;
  }
  try {
    const { data, error: err } = await api.PUT(
      "/api/v1/me/remy/context-sources/{source_key}",
      {
        params: { path: { source_key: key } },
        body: { source_mode: mode } as ContextSourceUpdate,
      },
    );
    if (err) {
      error.value = `Failed to update source: ${formatApiError(err)}`;
      if (prev && prevMode) {
        prev.source_mode = prevMode;
      }
      return;
    }
    if (data && prev) {
      sources.value = data as ContextSourceItem[];
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : "Failed to update source";
    if (prev && prevMode) {
      prev.source_mode = prevMode;
    }
  } finally {
    savingKey.value = null;
  }
}

async function resetToDefaults() {
  resetting.value = true;
  error.value = null;
  try {
    const { error: err } = await api.DELETE("/api/v1/me/remy/context-sources");
    if (err) {
      error.value = `Failed to reset sources: ${formatApiError(err)}`;
      return;
    }
    await fetchSources();
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : "Failed to reset sources";
  } finally {
    resetting.value = false;
  }
}

onMounted(() => {
  fetchSources();
});
</script>

<style scoped>
@reference "../../style.css";
.remy-cs-select {
  @apply rounded-lg px-2 py-1.5 text-xs outline-none;
  background-color: hsl(var(--background));
  border: 1px solid hsl(var(--input));
  color: hsl(var(--foreground));
  min-width: 100px;
}
.remy-cs-select:focus {
  border-color: hsl(var(--ring));
  box-shadow: 0 0 0 1px hsla(var(--ring) / 0.3);
}
.remy-cs-select:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.remy-cs-row {
  transition: background-color 150ms ease;
}
.remy-cs-row:hover {
  background-color: hsl(var(--accent));
}
</style>
