<template>
  <div class="page-wide">
    <PageTabs :tabs="[
      { label: $t('views.SchemaInferenceView.browse'), to: '/schemas' },
      { label: $t('views.SchemaInferenceView.editor'), to: '/schemas/editor' },
      { label: $t('views.SchemaInferenceView.infer'), to: '/schemas/infer' },
    ]" />
    <PageHeader :title="$t('views.SchemaInferenceView.schema_inference')" :subtitle="$t('views.SchemaInferenceView.infer_a_schema_from_a_connected_data_source')" />

    <div
      v-if="loadingConnectors"
      data-testid="schema-inference-loading-skeleton"
      aria-busy="true"
      class="space-y-4"
    >
      <section class="rounded-lg border bg-card p-6 shadow-sm">
        <SkeletonBlock height-class="h-5 w-32" />
        <SkeletonBlock class="mt-4" height-class="h-10 w-full" />
        <SkeletonBlock class="mt-4" height-class="h-10 w-full" />
        <SkeletonBlock class="mt-4" height-class="h-16 w-full" />
        <SkeletonBlock class="mt-4" height-class="h-10 w-32" />
      </section>
    </div>

    <ErrorAlert v-else-if="connectorsError" :message="connectorsError" />

    <template v-else>
      <section class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.SchemaInferenceView.source') }}</h2>
        <div class="space-y-4">
          <div>
            <label for="schemainferenceview-connector" class="mb-1 block text-sm font-medium">{{ $t('views.SchemaInferenceView.connector') }}</label>
            <Select
  :aria-label="$t('views.SchemaInferenceView.connector')"
  v-model="selectedConnectorId"
  :placeholder="$t('views.SchemaInferenceView.select_a_connector')"
  data-testid="schema-inference-connector"
  id="schemainferenceview-connector"
  class="w-full"
  :options="connectors.map(connector => ({ value: connector.id, label: connector.name + '(' + connector.connector_type_id + ')' }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
            <p v-if="connectors.length === 0" class="mt-2 text-sm text-muted-foreground">
              {{ $t('views.SchemaInferenceView.no_connectors_available') }}
            </p>
          </div>

          <div>
            <label for="schemainferenceview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.SchemaInferenceView.resource_type') }}</label>
            <input id="schemainferenceview-field-2"
              v-model="resourceType"
              type="text"
              data-testid="schema-inference-resource-type"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :placeholder="$t('views.SchemaInferenceView.eg_issues_repositories_pullrequests')"
            />
          </div>

          <div>
            <label for="schemainferenceview-field-1" class="mb-1 block text-sm font-medium">
              {{ $t('views.SchemaInferenceView.sample_query') }}
              <span class="text-muted-foreground"> {{ $t('views.SchemaInferenceView.optional') }}</span>
            </label>
            <textarea id="schemainferenceview-field-1"
              v-model="sampleQuery"
              rows="2"
              data-testid="schema-inference-sample-query"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :placeholder="$t('views.SchemaInferenceView.eg_stateopensortupdatede')"
            />
          </div>

          <div class="flex items-center gap-2">
            <Button :disabled="!selectedConnectorId || !resourceType.trim() || inferring" data-testid="schema-inference-infer-schema" @click="inferSchema">
              {{ inferring ? $t('views.SchemaInferenceView.inferring') : $t('views.SchemaInferenceView.infer_schema') }}
            </Button>
          </div>
        </div>
        <div v-if="inferError" class="mt-3 text-sm text-destructive">{{ inferError }}</div>
      </section>

      <section v-if="draftSchema" class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.SchemaInferenceView.draft_schema') }}</h2>

        <div class="mb-3">
          <span class="block text-sm font-medium text-muted-foreground">{{ $t('views.SchemaInferenceView.name_label') }}</span>
          <p class="text-sm">{{ draftSchema.name }}</p>
        </div>

        <div class="mb-3">
          <span class="block text-sm font-medium text-muted-foreground">{{ $t('views.SchemaInferenceView.description_label') }}</span>
          <p class="text-sm">{{ draftSchema.description ?? '—' }}</p>
        </div>

        <div class="mb-4">
          <span class="mb-2 block text-sm font-medium text-muted-foreground">{{ $t('views.SchemaInferenceView.fields_label') }}</span>
          <table v-if="draftSchema.fields.length > 0" class="w-full text-sm">
            <thead>
              <tr class="border-b text-left text-muted-foreground">
                <th class="pb-2 font-medium">{{ $t('views.SchemaInferenceView.field_name') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SchemaInferenceView.field_type') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SchemaInferenceView.field_required') }}</th>
                <th class="pb-2 font-medium">{{ $t('views.SchemaInferenceView.field_description') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="field in draftSchema.fields" :key="field.name" class="border-b last:border-0">
                <td class="py-2 font-mono text-xs">{{ field.name }}</td>
                <td class="py-2 font-mono text-xs text-muted-foreground">{{ field.type }}</td>
                <td class="py-2">
                  <span
                    class="inline-block rounded px-1.5 py-0.5 text-xs font-medium"
                    :class="field.required ? 'bg-primary/10 text-primary' : 'bg-muted text-muted-foreground'"
                  >
                    {{ field.required ? $t('views.SchemaInferenceView.yes') : $t('views.SchemaInferenceView.no') }}
                  </span>
                </td>
                <td class="py-2 text-xs text-muted-foreground">{{ field.description ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="text-sm text-muted-foreground">{{ $t('views.SchemaInferenceView.no_fields_inferred') }}</p>
        </div>

        <div class="mb-4">
          <button type="button"
            data-testid="schema-inference-toggle-raw-json"
            class="flex items-center gap-1 text-sm text-primary hover:underline"
            :aria-expanded="showRawJson"
            aria-controls="schema-inference-raw-json"
            @click="showRawJson = !showRawJson"
          >
            <ChevronRight
              class="h-4 w-4 transition-transform"
              :class="{ 'rotate-90': showRawJson }"
              aria-hidden="true"
            />
            {{ showRawJson ? $t('common.hide') : $t('common.show') }} {{ $t('views.SchemaInferenceView.raw_json') }}
          </button>
          <div id="schema-inference-raw-json">
            <JsonViewer v-if="showRawJson" :data="rawDefinitionJson" :show-toolbar="true" :max-height="'24rem'" />
          </div>
        </div>

        <div class="flex items-center gap-2">
          <Button :disabled="publishing" data-testid="schema-inference-publish" @click="publishSchema">
            {{ publishing ? $t('views.SchemaInferenceView.publishing') : $t('views.SchemaInferenceView.publish') }}
          </Button>
          <button type="button"
            data-testid="schema-inference-discard"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            @click="resetForm"
          >
            {{ $t('views.SchemaInferenceView.discard') }}
          </button>
        </div>
        <div v-if="publishError" class="mt-3 text-sm text-destructive">{{ publishError }}</div>
        <div v-if="publishSuccess" class="mt-3 text-sm text-success">{{ publishSuccess }}</div>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ChevronRight } from '@lucide/vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import SkeletonBlock from '../components/shared/SkeletonBlock.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import JsonViewer from '../components/shared/JsonViewer.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import PageTabs from "../components/PageTabs.vue"
import Button from 'primevue/button'
import Select from '../components/shared/AppSelect.vue'

type ConnectorItem = components['schemas']['ConnectorResponse']

interface DraftSchema {
  name: string
  description: string | null
  fields: Array<{name: string; type: string; required: boolean; description: string | null}>
}

const router = useRouter()
const { t } = useI18n()

const connectors = ref<ConnectorItem[]>([])
const loadingConnectors = ref(true)
const connectorsError = ref<string | null>(null)

const selectedConnectorId = ref('')
const resourceType = ref('')
const sampleQuery = ref('')

const inferring = ref(false)
const inferError = ref<string | null>(null)
const draftSchema = ref<DraftSchema | null>(null)
const rawDefinitionJson = ref<Record<string, unknown> | null>(null)

const publishing = ref(false)
const publishError = ref<string | null>(null)
const publishSuccess = ref<string | null>(null)

const showRawJson = ref(false)

async function loadConnectors() {
  loadingConnectors.value = true
  connectorsError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/connectors')
    if (err) {
      connectorsError.value = t('views.SchemaInferenceView.failed_to_load_connectors', { error: formatApiError(err) })
    } else if (data) {
      connectors.value = data.items
    }
  } catch (e: unknown) {
    connectorsError.value = t('views.SchemaInferenceView.failed_to_load_connectors', { error: formatApiError(e) })
  } finally {
    loadingConnectors.value = false
  }
}

function extractFieldsFromDefinition(def: Record<string, unknown>): DraftSchema['fields'] {
  const properties = (def.properties as Record<string, unknown>) || {}
  const required = (def.required as string[]) || []
  return Object.entries(properties).map(([name, schema]) => {
    const s = schema as Record<string, unknown>
    return {
      name,
      type: (s.type as string) || 'string',
      required: required.includes(name),
      description: (s.description as string) || null,
    }
  })
}

async function inferSchema() {
  if (!selectedConnectorId.value || !resourceType.value.trim()) return
  inferring.value = true
  inferError.value = null
  draftSchema.value = null
  rawDefinitionJson.value = null
  showRawJson.value = false
  try {
    const { data, error: err } = await api.POST('/api/v1/schemas/infer', {
      body: {
        connector_instance_id: selectedConnectorId.value,
        sample_query: {
          resource: resourceType.value.trim(),
          filters: {},
          limit: 200,
          query: sampleQuery.value.trim() || undefined,
        },
      },
    })
    if (err) {
      inferError.value = t('views.SchemaInferenceView.schema_inference_failed', { error: formatApiError(err) })
    } else if (data) {
      rawDefinitionJson.value = data.definition_json
      draftSchema.value = {
        name: data.suggestion_name,
        description: data.suggestion_description ?? null,
        fields: extractFieldsFromDefinition(data.definition_json),
      }
    }
  } catch (e: unknown) {
    inferError.value = t('views.SchemaInferenceView.schema_inference_failed', { error: formatApiError(e) })
  } finally {
    inferring.value = false
  }
}

async function publishSchema() {
  if (!draftSchema.value) return
  publishing.value = true
  publishError.value = null
  publishSuccess.value = null
  try {
    const { data: schemaData, error: schemaErr } = await api.POST('/api/v1/schemas', {
      body: {
        name: draftSchema.value.name,
        description: draftSchema.value.description,
      },
    })
    if (schemaErr) {
      publishError.value = t('views.SchemaInferenceView.publish_failed', { error: formatApiError(schemaErr) })
      return
    }
    if (!schemaData) {
      publishError.value = t('views.SchemaInferenceView.publish_failed_no_response')
      return
    }

    const { error: versionErr } = await api.POST('/api/v1/schemas/{schema_id}/versions', {
      params: { path: { schema_id: schemaData.id } },
      body: {
        version: 'v1',
        version_number: 1,
        definition_json: rawDefinitionJson.value || { type: 'object', properties: {} },
        published: true,
      },
    })
    if (versionErr) {
      publishError.value = t('views.SchemaInferenceView.publish_failed', { error: formatApiError(versionErr) })
      return
    }

    publishSuccess.value = t('views.SchemaInferenceView.schema_published', { name: schemaData.name })
    setTimeout(() => {
      router.push({ name: 'library' })
    }, 1500)
  } catch (e: unknown) {
    publishError.value = t('views.SchemaInferenceView.publish_failed', { error: formatApiError(e) })
  } finally {
    publishing.value = false
  }
}

function resetForm() {
  draftSchema.value = null
  rawDefinitionJson.value = null
  showRawJson.value = false
  inferError.value = null
  publishError.value = null
  publishSuccess.value = null
}

onMounted(() => {
  loadConnectors()
})
</script>
