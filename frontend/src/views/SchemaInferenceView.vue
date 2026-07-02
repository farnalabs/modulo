<template>
  <PageTabs :tabs="[
    { label: 'Browse', to: '/schemas' },
    { label: 'Editor', to: '/schemas/editor' },
    { label: 'Infer', to: '/schemas/infer' },
  ]" />
  <div class="mx-auto max-w-4xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.SchemaInferenceView.schema_inference') }}</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.SchemaInferenceView.infer_a_schema_from_a_connected_data_source') }}</p>
    </header>

    <LoadingSpinner v-if="loadingConnectors" />

    <ErrorAlert v-else-if="connectorsError" :message="connectorsError" />

    <template v-else>
      <section class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="mb-4 text-lg font-semibold">Source</h2>
        <div class="space-y-4">
          <div>
            <label class="mb-1 block text-sm font-medium">Connector</label>
            <select
              v-model="selectedConnectorId"
              data-testid="schema-inference-connector"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <option value="" disabled>{{ $t('views.SchemaInferenceView.select_a_connector') }}</option>
              <option
                v-for="connector in connectors"
                :key="connector.id"
                :value="connector.id"
              >
                {{ connector.name }} ({{ connector.connector_type }})
              </option>
            </select>
            <p v-if="connectors.length === 0" class="mt-2 text-sm text-muted-foreground">
              No connectors available. Create one first.
            </p>
          </div>

          <div>
            <label class="mb-1 block text-sm font-medium">{{ $t('views.SchemaInferenceView.resource_type') }}</label>
            <input
              v-model="resourceType"
              type="text"
              data-testid="schema-inference-resource-type"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :placeholder="$t('views.SchemaInferenceView.eg_issues_repositories_pullrequests')"
            />
          </div>

          <div>
            <label class="mb-1 block text-sm font-medium">
              Sample query
              <span class="text-muted-foreground"> (optional)</span>
            </label>
            <textarea
              v-model="sampleQuery"
              rows="2"
              data-testid="schema-inference-sample-query"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :placeholder="$t('views.SchemaInferenceView.eg_stateopensortupdated')"
            />
          </div>

          <div class="flex items-center gap-2">
            <button
              :disabled="!selectedConnectorId || !resourceType.trim() || inferring"
              data-testid="schema-inference-infer-schema"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              @click="inferSchema"
            >
              {{ inferring ? 'Inferring...' : 'Infer Schema' }}
            </button>
          </div>
        </div>
        <div v-if="inferError" class="mt-3 text-sm text-destructive">{{ inferError }}</div>
      </section>

      <section v-if="draftSchema" class="rounded-lg border bg-card p-6 shadow-sm">
        <h2 class="mb-4 text-lg font-semibold">{{ $t('views.SchemaInferenceView.draft_schema') }}</h2>

        <div class="mb-3">
          <label class="block text-sm font-medium text-muted-foreground">Name</label>
          <p class="text-sm">{{ draftSchema.name }}</p>
        </div>

        <div v-if="draftSchema.description" class="mb-3">
          <label class="block text-sm font-medium text-muted-foreground">Description</label>
          <p class="text-sm">{{ draftSchema.description }}</p>
        </div>

        <div class="mb-4">
          <label class="mb-2 block text-sm font-medium text-muted-foreground">Fields</label>
          <table v-if="draftSchema.fields.length > 0" class="w-full text-sm">
            <thead>
              <tr class="border-b text-left text-muted-foreground">
                <th class="pb-2 font-medium">Name</th>
                <th class="pb-2 font-medium">Type</th>
                <th class="pb-2 font-medium">Required</th>
                <th class="pb-2 font-medium">Description</th>
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
                    {{ field.required ? 'yes' : 'no' }}
                  </span>
                </td>
                <td class="py-2 text-xs text-muted-foreground">{{ field.description ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="text-sm text-muted-foreground">{{ $t('views.SchemaInferenceView.no_fields_inferred') }}</p>
        </div>

        <div class="mb-4">
          <button
            data-testid="schema-inference-toggle-raw-json"
            class="flex items-center gap-1 text-sm text-primary hover:underline"
            @click="showRawJson = !showRawJson"
          >
            <svg
              class="h-4 w-4 transition-transform"
              :class="{ 'rotate-90': showRawJson }"
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="m9 18 6-6-6-6" />
            </svg>
            {{ showRawJson ? 'Hide' : 'Show' }} raw JSON
          </button>
          <pre v-if="showRawJson" class="mt-2 overflow-x-auto rounded-lg bg-muted p-4 text-xs">{{ formattedJson }}</pre>
        </div>

        <div class="flex items-center gap-2">
          <button
            :disabled="publishing"
            data-testid="schema-inference-publish"
            class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            @click="publishSchema"
          >
            {{ publishing ? 'Publishing...' : 'Publish' }}
          </button>
          <button
            data-testid="schema-inference-discard"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            @click="resetForm"
          >
            Discard
          </button>
        </div>
        <div v-if="publishError" class="mt-3 text-sm text-destructive">{{ publishError }}</div>
        <div v-if="publishSuccess" class="mt-3 text-sm text-success">{{ publishSuccess }}</div>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import PageTabs from "../components/PageTabs.vue"

type ConnectorItem = components['schemas']['ConnectorItem']
type SchemaInferResponse = components['schemas']['SchemaInferResponse']

const router = useRouter()

const connectors = ref<ConnectorItem[]>([])
const loadingConnectors = ref(true)
const connectorsError = ref<string | null>(null)

const selectedConnectorId = ref('')
const resourceType = ref('')
const sampleQuery = ref('')

const inferring = ref(false)
const inferError = ref<string | null>(null)
const draftSchema = ref<SchemaInferResponse | null>(null)

const publishing = ref(false)
const publishError = ref<string | null>(null)
const publishSuccess = ref<string | null>(null)

const showRawJson = ref(false)

const formattedJson = computed(() => {
  if (!draftSchema.value) return ''
  return JSON.stringify(draftSchema.value, null, 2)
})

async function loadConnectors() {
  loadingConnectors.value = true
  connectorsError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/connectors')
    if (err) {
      connectorsError.value = `Failed to load connectors: ${err}`
    } else if (data) {
      connectors.value = data.items
    }
  } catch (e: unknown) {
    connectorsError.value = `Failed to load connectors: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loadingConnectors.value = false
  }
}

async function inferSchema() {
  if (!selectedConnectorId.value || !resourceType.value.trim()) return
  inferring.value = true
  inferError.value = null
  draftSchema.value = null
  showRawJson.value = false
  try {
    const { data, error: err } = await api.POST('/api/v1/schemas/infer', {
      body: {
        connector_instance_id: selectedConnectorId.value,
        resource_type: resourceType.value.trim(),
        sample_query: sampleQuery.value.trim() || null,
      },
    })
    if (err) {
      inferError.value = `Schema inference failed: ${err}`
    } else if (data) {
      draftSchema.value = data
    }
  } catch (e: unknown) {
    inferError.value = `Schema inference failed: ${e instanceof Error ? e.message : String(e)}`
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
    const { data, error: err } = await api.POST('/api/v1/schemas', {
      body: {
        name: draftSchema.value.name,
        description: draftSchema.value.description,
        fields: draftSchema.value.fields,
      },
    })
    if (err) {
      publishError.value = `Publish failed: ${err}`
    } else if (data) {
      publishSuccess.value = `Schema "${data.name}" published.`
      setTimeout(() => {
        router.push({ name: 'library' })
      }, 1500)
    }
  } catch (e: unknown) {
    publishError.value = `Publish failed: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    publishing.value = false
  }
}

function resetForm() {
  draftSchema.value = null
  showRawJson.value = false
  inferError.value = null
  publishError.value = null
  publishSuccess.value = null
}

onMounted(() => {
  loadConnectors()
})
</script>
