<template>
  <FeatureGate feature-name="plugin_management" show-disabled>
    <div class="page-wide">
      <header class="flex items-center justify-between">
        <PageHeader title="Plugins" :subtitle="$t('views.AdminPluginsView.manage_installed_modulo_plugins_and_extensions')" />
        <Button class="border-primary/30 hover:border-primary/60" data-testid="admin-plugins-refresh" @click="loadPlugins">
          Refresh
        </Button>
      </header>

      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadPlugins" />

      <template v-else>
        <div v-if="plugins.length === 0" class="card p-8 text-center">
          <p class="text-lg font-medium">{{ $t('views.AdminPluginsView.no_plugins_installed') }}</p>
          <p class="mt-1 text-sm text-muted-foreground">
            Install plugins via pip to extend Modulo with additional connectors and model backends.
          </p>
        </div>

        <div class="table-wrapper">
          <table class="w-full text-left text-sm">
            <thead>
              <tr>
                <th class="table-header">{{ $t('views.AdminPluginsView.plugin') }}</th>
                <th class="table-header">{{ $t('views.AdminPluginsView.version') }}</th>
                <th class="table-header">{{ $t('views.AdminPluginsView.type') }}</th>
                <th class="table-header capitalize">{{ $t('views.AdminPluginsView.status') }}</th>
                <th class="table-header table-cell-numeric">{{ $t('views.AdminPluginsView.actions') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <template v-for="plugin in plugins" :key="plugin.PLUGIN_ID">
                <tr
                  class="hover:bg-muted/30 transition-colors"
                  :class="{ 'opacity-60': !activeStates[plugin.PLUGIN_ID] }"
                >
                  <td class="table-cell">
                    <div>
                      <button
                        type="button"
                        class="font-medium text-left hover:text-primary transition-colors"
                        @click="toggleExpand(plugin.PLUGIN_ID)"
                      >
                        {{ plugin.display_name }}
                      </button>
                      <p class="mt-0.5 text-xs text-muted-foreground truncate max-w-xs">
                        {{ plugin.description }}
                      </p>
                    </div>
                  </td>
                  <td class="table-cell font-mono text-xs text-muted-foreground">
                    {{ plugin.version }}
                  </td>
                  <td class="table-cell">
                    <span
                      class="rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary"
                    >
                      {{ pluginTypeLabel(plugin.capabilities) }}
                    </span>
                  </td>
                  <td class="table-cell">
                    <span
                      class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
                      :class="activeStates[plugin.PLUGIN_ID] !== false && plugin.health_ok ? 'bg-success/10 text-success' : 'bg-muted text-muted-foreground'"
                    >
                      <span
                        class="h-1.5 w-1.5 rounded-full"
                        :class="activeStates[plugin.PLUGIN_ID] !== false && plugin.health_ok ? 'bg-success' : 'bg-muted-foreground'"
                      />
                      {{ activeStates[plugin.PLUGIN_ID] !== false && plugin.health_ok ? 'Active' : 'Inactive' }}
                    </span>
                  </td>
                  <td class="table-cell-numeric">
                    <div class="flex items-center justify-end gap-1">
                      <label for="adminpluginsview-field-1"
                        class="relative inline-flex cursor-pointer items-center"
                        :title="activeStates[plugin.PLUGIN_ID] !== false ? 'Disable plugin' : 'Enable plugin'"
                        :aria-label="activeStates[plugin.PLUGIN_ID] !== false ? 'Disable plugin' : 'Enable plugin'"
                      >
                        <input id="adminpluginsview-field-1"
                          type="checkbox"
                          class="sr-only peer"
                          :checked="activeStates[plugin.PLUGIN_ID] !== false"
                          @change="togglePlugin(plugin.PLUGIN_ID)"
                        />
                        <div
                          class="peer h-5 w-9 rounded-full bg-muted-foreground/30 after:absolute after:start-[2px] after:top-[2px] after:h-4 after:w-4 after:rounded-full after:bg-background after:transition-all peer-checked:bg-primary peer-checked:after:translate-x-full"
                        />
                      </label>
                      <button
                        type="button"
                        class="rounded p-1 text-muted-foreground hover:bg-accent"
                        data-testid="admin-plugins-expand"
                        :aria-label="$t('views.AdminPluginsView.expand_plugin_details')"
                        :title="expanded[plugin.PLUGIN_ID] ? 'Collapse details' : 'Expand details'"
                        @click="toggleExpand(plugin.PLUGIN_ID)"
                      >
                        <svg
                          class="h-4 w-4 transition-transform"
                          :class="{ 'rotate-180': expanded[plugin.PLUGIN_ID] }"
                          xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                        >
                          <polyline points="6 9 12 15 18 9" />
                        </svg>
                      </button>
                    </div>
                  </td>
                </tr>
                <tr v-if="expanded[plugin.PLUGIN_ID]">
                  <td colspan="5" class="bg-muted/20 px-4 py-4">
                    <div class="space-y-3">
                      <div class="grid grid-cols-2 gap-4">
                        <div>
                          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminPluginsView.plugin_id') }}</span>
                          <p class="font-mono text-xs">{{ plugin.PLUGIN_ID }}</p>
                        </div>
                        <div>
                          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminPluginsView.description') }}</span>
                          <p class="text-sm">{{ plugin.description || '—' }}</p>
                        </div>
                        <div>
                          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminPluginsView.capabilities') }}</span>
                          <div class="flex flex-wrap gap-1 mt-1">
                            <span
                              v-for="cap in plugin.capabilities"
                              :key="cap"
                              class="rounded-full bg-secondary/50 px-2 py-0.5 text-xs font-medium"
                            >
                              {{ cap }}
                            </span>
                            <span v-if="!plugin.capabilities || plugin.capabilities.length === 0" class="text-xs text-muted-foreground">—</span>
                          </div>
                        </div>
                        <div>
                          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminPluginsView.health_detail') }}</span>
                          <p
                            class="text-sm"
                            :class="plugin.health_ok ? 'text-success' : 'text-destructive'"
                          >
                            {{ plugin.health_detail || '—' }}
                          </p>
                        </div>
                        <div>
                          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminPluginsView.last_checked') }}</span>
                          <p class="text-sm">
                            {{ plugin.health_checked_at ? new Date(plugin.health_checked_at).toLocaleString() : '—' }}
                          </p>
                        </div>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
      </template>
    </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { reactive, computed, watch } from 'vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'
import Button from 'primevue/button'

interface PluginItem {
  PLUGIN_ID: string
  display_name: string
  description: string
  version: string
  capabilities: string[]
  health_ok: boolean
  health_detail: string
  health_checked_at: string | null
}

const { data: pluginsData, loading, error, load: loadPlugins } = useDataFetch(
  () => (api as any).GET('/api/v1/plugins') as Promise<{ data?: PluginItem[]; error?: { detail?: string } }>,
  { initialValue: [] as PluginItem[] }
)

const plugins = computed(() => pluginsData.value ?? [])

watch(pluginsData, (newData) => {
  if (newData) {
    for (const p of newData) {
      if (activeStates[p.PLUGIN_ID] === undefined) {
        activeStates[p.PLUGIN_ID] = true
      }
    }
  }
})

const activeStates = reactive<Record<string, boolean>>({})
const expanded = reactive<Record<string, boolean>>({})

function pluginTypeLabel(capabilities: string[]): string {
  if (capabilities.includes('model_backend')) return 'model_backend'
  if (capabilities.includes('connector_type')) return 'connector'
  return 'core'
}

function toggleExpand(id: string) {
  expanded[id] = !expanded[id]
}

async function togglePlugin(id: string) {
  const newState = activeStates[id] === false
  activeStates[id] = newState
  try {
    const { error: err } = await (api as any).PUT(`/api/v1/plugins/${id}/toggle`, {
      body: { active: newState }
    })
    if (err) {
      activeStates[id] = !newState
      error.value = `Failed to toggle plugin: ${formatApiError(err)}`
    }
  } catch (e: unknown) {
    activeStates[id] = !newState
    error.value = `Failed to toggle plugin: ${formatApiError(e)}`
  }
}

</script>
