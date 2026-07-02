<template>
  <PageTabs :tabs="[
    { label: 'Browse', to: '/schemas' },
    { label: 'Editor', to: '/schemas/editor' },
    { label: 'Infer', to: '/schemas/infer' },
  ]" />
  <div class="mx-auto max-w-4xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Schemas</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.SchemaListView.manage_schemas_and_deprecate_outdated_definitions') }}</p>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" />

    <template v-else>
      <div v-if="schemas.length === 0" class="card p-8 text-center">
        <p class="text-lg font-medium">{{ $t('views.SchemaListView.no_schemas_found') }}</p>
        <p class="mt-1 text-sm text-muted-foreground">
          Schemas are created through inference or direct creation.
        </p>
      </div>

      <div class="overflow-hidden rounded-lg border">
        <table class="w-full text-left text-sm">
          <thead class="bg-muted/50">
            <tr>
              <th class="px-4 py-3 font-medium">Name</th>
              <th class="px-4 py-3 font-medium">Description</th>
              <th class="px-4 py-3 font-medium">Status</th>
              <th class="px-4 py-3 font-medium text-right">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="schema in schemas"
              :key="schema.id"
              class="hover:bg-muted/30 transition-colors"
            >
              <td class="px-4 py-3 font-medium">{{ schema.name }}</td>
              <td class="px-4 py-3 text-muted-foreground">{{ schema.description || '—' }}</td>
              <td class="px-4 py-3">
                <span
                  v-if="schema.deprecated"
                  class="inline-flex items-center gap-1 rounded-full bg-destructive/10 px-2.5 py-0.5 text-xs font-medium text-destructive"
                >
                  <span class="h-1.5 w-1.5 rounded-full bg-destructive" />
                  Deprecated
                </span>
                <span
                  v-else
                  class="inline-flex items-center gap-1 rounded-full bg-success/10 px-2.5 py-0.5 text-xs font-medium text-success"
                >
                  <span class="h-1.5 w-1.5 rounded-full bg-success" />
                  Active
                </span>
              </td>
              <td class="px-4 py-3 text-right">
                <div v-if="!schema.deprecated" class="flex items-center justify-end gap-1">
                  <button
                    class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-destructive"
                    data-testid="schema-deprecate"
                    :aria-label="$t('views.SchemaListView.deprecate_schema')"
                    :title="$t('views.SchemaListView.deprecate_schema_1')"
                    @click="confirmDeprecate(schema)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <circle cx="12" cy="12" r="10" />
                      <line x1="12" y1="8" x2="12" y2="12" />
                      <line x1="12" y1="16" x2="12.01" y2="16" />
                    </svg>
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="deprecateConfirmId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
        <p class="text-sm font-medium text-destructive">Deprecate "{{ deprecateConfirmName }}"?</p>
        <p class="mt-1 text-sm text-destructive/80">
          This schema will be marked as deprecated. Agents using it will still function, but it will no longer appear as active.
        </p>
        <div class="mt-3 flex items-center gap-2">
          <button
            :disabled="deprecating"
            class="rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:brightness-110 disabled:opacity-50 transition-all"
            data-testid="schema-deprecate-confirm"
            @click="deprecateSchema"
          >
            {{ deprecating ? 'Deprecating...' : 'Deprecate' }}
          </button>
          <button
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="schema-deprecate-cancel"
            @click="deprecateConfirmId = null"
          >
            Cancel
          </button>
        </div>
        <div v-if="deprecateError" class="mt-2 text-sm text-destructive">{{ deprecateError }}</div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError, type ProblemDetail } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import PageTabs from "../components/PageTabs.vue"

type SchemaItem = components['schemas']['SchemaItem']

const schemas = ref<SchemaItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

const deprecateConfirmId = ref<string | null>(null)
const deprecateConfirmName = ref('')
const deprecating = ref(false)
const deprecateError = ref<string | null>(null)

async function loadSchemas() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/schemas', {
      params: { query: { page: 1, page_size: 100 } },
    })
    if (err) {
      error.value = err && typeof err === 'object' && 'detail' in err
        ? `Failed to load schemas: ${(err as ProblemDetail).detail}`
        : `Failed to load schemas: ${formatApiError(err)}`
    } else if (data) {
      schemas.value = data.items
    }
  } catch (e: unknown) {
    error.value = `Failed to load schemas: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

function confirmDeprecate(schema: SchemaItem) {
  deprecateConfirmId.value = schema.id
  deprecateConfirmName.value = schema.name
  deprecateError.value = null
}

async function deprecateSchema() {
  if (!deprecateConfirmId.value) return
  deprecating.value = true
  deprecateError.value = null
  try {
    const { data, error: err } = await api.PATCH('/api/v1/schemas/{schema_id}/deprecate', {
      params: { path: { schema_id: deprecateConfirmId.value } },
    })
    if (err) {
      deprecateError.value = String(err)
    } else if (data) {
      const idx = schemas.value.findIndex(s => s.id === deprecateConfirmId.value)
      if (idx >= 0) {
        schemas.value[idx] = data
      }
      deprecateConfirmId.value = null
    }
  } catch (e: unknown) {
    deprecateError.value = e instanceof Error ? e.message : String(e)
  } finally {
    deprecating.value = false
  }
}

onMounted(loadSchemas)
</script>
