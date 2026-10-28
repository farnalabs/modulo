<template>
  <div class="flex h-[calc(100vh-3.5rem)] flex-col">
    <PageHeader
      :title="$t('views.SchemaEditorView.schemas')"
      :tabs="[
        { label: $t('views.SchemaInferenceView.browse'), to: '/schemas' },
        { label: $t('views.SchemaInferenceView.editor'), to: '/schemas/editor' },
        { label: $t('views.SchemaInferenceView.infer'), to: '/schemas/infer' },
      ]"
    />
    <div class="flex min-h-0 flex-1">
    <SchemaEditorSidebar
      :schemas="filteredSchemas"
      :loading="loadingSchemas"
      :selected-id="selectedSchemaId"
      :search-query="searchQuery"
      @select="selectSchema"
      @create="createNewSchema"
      @update:search-query="searchQuery = $event"
    />

    <!-- FAR-1913: axe `scrollable-region-focusable` requires this scroll region
         to be reachable by keyboard. The editing form already contains focusable
         controls; the empty/`:id`-not-found state did not, so it now offers a
         real CTA button. That is the focusable child the rule wants — putting
         `tabindex` on `<main>` itself trips SonarCloud Web:S6845 (tabindex
         belongs on interactive elements). -->
    <main class="flex-1 overflow-y-auto">
      <div v-if="!editingSchema" class="p-6">
        <EmptyState :title="$t('views.SchemaEditorView.select_or_create')">
          <Button data-testid="schema-editor-empty-new" @click="createNewSchema">
            {{ $t('views.SchemaEditorView.new_schema') }}
          </Button>
        </EmptyState>
      </div>

      <template v-else>
        <div class="space-y-6 p-6">
          <PageHeader :title="isNew ? $t('views.SchemaEditorView.new_schema_title') : $t('views.SchemaEditorView.edit_schema_title')" :subtitle="isNew ? $t('views.SchemaEditorView.define_new_schema') : schemaName">
            <template #right>
              <Button data-testid="schema-editor-save" :disabled="saving || !isValid" @click="saveSchema">
                {{ saving ? $t('views.SchemaEditorView.saving') : $t('views.SchemaEditorView.save') }}
              </Button>
              <button type="button"
                data-testid="schema-editor-cancel"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                @click="cancelEditing"
              >
                {{ $t('views.SchemaEditorView.cancel') }}
              </button>
            </template>
          </PageHeader>

          <div v-if="validationErrors.length > 0" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
            <p class="mb-2 text-sm font-medium text-destructive-text">{{ $t('views.SchemaEditorView.validation_errors') }}</p>
            <ul class="list-inside list-disc space-y-1 text-sm text-destructive-text">
              <li v-for="err in validationErrors" :key="err">{{ err }}</li>
            </ul>
          </div>

          <!--
            saveError carries both schema-load and save failures; a Retry would
            refetch the list, not resubmit the form, so none is offered — the
            user re-clicks Save (or reloads) after reading the message.
          -->
          <ErrorAlert v-if="saveError" :message="saveError" :retryable="false" />

          <div v-if="saveSuccess" class="rounded-lg border border-success/50 bg-success/10 p-4 text-sm text-success-text">
            {{ saveSuccess }}
          </div>

          <div class="grid grid-cols-1 gap-6 xl:grid-cols-2">
            <SchemaEditorForm
              v-model:name="schemaName"
              v-model:description="schemaDescription"
              v-model:version="schemaVersion"
              v-model:fields="fields"
            />

            <div class="space-y-6">
              <FeatureGate feature-name="schema_version_history" required-tier="team" show-disabled>
                <SchemaVersionHistory
                  :versions="versions"
                  :loading="loadingVersions"
                  @restore="restoreVersion"
                />
              </FeatureGate>

              <SchemaJsonPreview :json="jsonPreview" @copy="copyJsonPreview" />
            </div>
          </div>
        </div>
      </template>
    </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { useProductAnalytics } from '../composables/useProductAnalytics'
import { ANALYTICS_EVENTS } from '../composables/productAnalyticsEvents'
import { buildJsonSchema, parseDefinitionToFields, type SchemaField } from '../utils/schema-definition'
import FeatureGate from '../components/FeatureGate.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import Button from 'primevue/button'
import SchemaEditorSidebar, { type SchemaItem } from '../components/schema/SchemaEditorSidebar.vue'
import SchemaEditorForm from '../components/schema/SchemaEditorForm.vue'
import SchemaVersionHistory, { type SchemaVersion } from '../components/schema/SchemaVersionHistory.vue'
import SchemaJsonPreview from '../components/schema/SchemaJsonPreview.vue'

const { t } = useI18n()

const route = useRoute()

const { track } = useProductAnalytics()

const schemas = ref<SchemaItem[]>([])
const loadingSchemas = ref(true)
const searchQuery = ref('')

const selectedSchemaId = ref<string | null>(null)
const editingSchema = ref(false)
const isNew = ref(false)

const schemaName = ref('')
const schemaDescription = ref('')
const schemaVersion = ref('1.0.0')
const fields = ref<SchemaField[]>([])

const versions = ref<SchemaVersion[]>([])
const loadingVersions = ref(false)

const saving = ref(false)
const saveError = ref<string | null>(null)
const saveSuccess = ref<string | null>(null)

const validationErrors = ref<string[]>([])

const filteredSchemas = computed(() => {
  if (!searchQuery.value.trim()) return schemas.value
  const q = searchQuery.value.toLowerCase()
  return schemas.value.filter(
    s => s.name.toLowerCase().includes(q) || (s.description ?? '').toLowerCase().includes(q),
  )
})

const isValid = computed(() => {
  if (!schemaName.value.trim()) return false
  if (!schemaVersion.value.trim()) return false
  if (fields.value.length === 0) return false
  return fields.value.every(f => f.name.trim())
})

const jsonPreview = computed(() => {
  return JSON.stringify(
    buildJsonSchema({
      schemaName: schemaName.value,
      schemaDescription: schemaDescription.value,
      fields: fields.value,
      untitledLabel: t('views.SchemaEditorView.untitled_schema'),
    }),
    null,
    2,
  )
})

async function loadSchemas() {
  loadingSchemas.value = true
  try {
    const { data, error } = await api.GET('/api/v1/schemas', {
      params: { query: { page: 1, page_size: 100 } },
    })
    if (error) {
      saveError.value = `${t('views.SchemaEditorView.failed_to_load_schemas')} ${formatApiError(error)}`
    } else if (data) {
      schemas.value = data.items
    }
  } catch (e: unknown) {
    saveError.value = `${t('views.SchemaEditorView.failed_to_load_schemas')} ${formatApiError(e)}`
  } finally {
    loadingSchemas.value = false
  }
}

function selectSchema(id: string) {
  selectedSchemaId.value = id
  const schema = schemas.value.find(s => s.id === id)
  if (!schema) return

  isNew.value = false
  editingSchema.value = true
  saveError.value = null
  saveSuccess.value = null
  validationErrors.value = []

  schemaName.value = schema.name
  schemaDescription.value = schema.description ?? ''
  schemaVersion.value = '1.0.0'
  fields.value = []

  loadLatestVersion(id)
  loadVersions(id)
}

function createNewSchema() {
  selectedSchemaId.value = null
  isNew.value = true
  editingSchema.value = true
  saveError.value = null
  saveSuccess.value = null
  validationErrors.value = []

  schemaName.value = ''
  schemaDescription.value = ''
  schemaVersion.value = '1.0.0'
  fields.value = []
  versions.value = []
}

function cancelEditing() {
  editingSchema.value = false
  selectedSchemaId.value = null
  isNew.value = false
}

async function loadLatestVersion(schemaId: string) {
  try {
    const { data, error } = await api.GET('/api/v1/schemas/{schema_id}/versions', {
      params: { path: { schema_id: schemaId }, query: { page: 1, page_size: 1 } },
    })
    if (error) return
    if (data.items && data.items.length > 0) {
      const latest = data.items[0]
      schemaVersion.value = latest.version
      fields.value = parseDefinitionToFields(latest.definition_json)
    }
  } catch (e) {
    console.warn('Failed to load schema', e)
  }
}

async function loadVersions(schemaId: string) {
  loadingVersions.value = true
  try {
    const { data, error } = await api.GET('/api/v1/schemas/{schema_id}/versions', {
      params: { path: { schema_id: schemaId }, query: { page: 1, page_size: 50 } },
    })
    if (error) return
    versions.value = data.items ?? []
  } catch (e: unknown) {
    console.warn('Failed to load versions', e)
    versions.value = []
  } finally {
    loadingVersions.value = false
  }
}

function collectLocalValidationErrors(): string[] {
  const errors: string[] = []
  if (!schemaName.value.trim()) errors.push(t('views.SchemaEditorView.schema_name_required'))
  if (!schemaVersion.value.trim()) errors.push(t('views.SchemaEditorView.schema_version_required'))
  if (fields.value.length === 0) errors.push(t('views.SchemaEditorView.at_least_one_field'))
  return errors
}

function appendFieldValidationErrors(errors: string[]): void {
  const seen = new Set<string>()
  for (const field of fields.value) {
    const name = field.name.trim()
    if (!name) {
      errors.push(t('views.SchemaEditorView.all_fields_must_have_name'))
      break
    }
    if (seen.has(name)) {
      errors.push(`${t('views.SchemaEditorView.duplicate_field_name')} "${name}"`)
    }
    seen.add(name)
  }
}

// Ask the backend to validate the schema in the JSON preview, appending any
// returned errors. Returns false only when the validation request itself
// failed (the caller then aborts without surfacing backend errors).
async function appendRemoteValidationErrors(errors: string[]): Promise<boolean> {
  try {
    const { data, error } = await api.POST('/api/v1/schemas/validate', {
      body: { definition: JSON.parse(jsonPreview.value) as Record<string, unknown> },
    })
    if (error) return false
    if (!data.valid && data.errors) {
      for (const e of data.errors) {
        errors.push(`${e.path}: ${e.message}`)
      }
    }
  } catch (e) {
    console.warn('Failed to validate schema', e)
  }
  return true
}

async function validateSchema(): Promise<boolean> {
  validationErrors.value = []
  const errors: string[] = collectLocalValidationErrors()
  appendFieldValidationErrors(errors)

  if (errors.length > 0) {
    validationErrors.value = errors
    return false
  }

  if (!(await appendRemoteValidationErrors(errors))) return false

  if (errors.length > 0) {
    validationErrors.value = errors
    return false
  }
  return true
}

function nextVersionNumber(): number {
  return versions.value.length > 0
    ? Math.max(...versions.value.map(v => v.version_number)) + 1
    : 1
}

async function createSchemaWithVersion(definitionJson: Record<string, unknown>): Promise<void> {
  const { data: schemaData, error: createErr } = await api.POST('/api/v1/schemas', {
    body: {
      name: schemaName.value.trim(),
      description: schemaDescription.value.trim() || null,
    },
  })
  if (createErr) {
    saveError.value = `${t('views.SchemaEditorView.create_failed')} ${formatApiError(createErr)}`
    return
  }
  if (!schemaData) return

  track(ANALYTICS_EVENTS.SCHEMA_CREATED, { schema_id: schemaData.id })

  const { error: versionErr } = await api.POST('/api/v1/schemas/{schema_id}/versions', {
    params: { path: { schema_id: schemaData.id } },
    body: {
      version: schemaVersion.value.trim(),
      version_number: 1,
      definition_json: definitionJson,
      published: true,
    },
  })
  if (versionErr) {
    saveError.value = t('views.SchemaEditorView.schema_created_version_failed')
    return
  }

  saveSuccess.value = `${t('views.SchemaEditorView.schema_created')} "${schemaData.name}"`
  await loadSchemas()
  selectedSchemaId.value = schemaData.id
  isNew.value = false
}

async function updateSchemaWithVersion(
  schemaId: string,
  definitionJson: Record<string, unknown>,
): Promise<void> {
  const { data: schemaData, error: updateErr } = await api.PATCH('/api/v1/schemas/{schema_id}', {
    params: { path: { schema_id: schemaId } },
    body: {
      name: schemaName.value.trim(),
      description: schemaDescription.value.trim() || null,
    },
  })
  if (updateErr) {
    saveError.value = `${t('views.SchemaEditorView.update_failed')} ${formatApiError(updateErr)}`
    return
  }

  const { error: versionErr } = await api.POST('/api/v1/schemas/{schema_id}/versions', {
    params: { path: { schema_id: schemaId } },
    body: {
      version: schemaVersion.value.trim(),
      version_number: nextVersionNumber(),
      definition_json: definitionJson,
      published: true,
    },
  })
  if (versionErr) {
    saveError.value = t('views.SchemaEditorView.schema_updated_version_failed')
    return
  }

  saveSuccess.value = `${t('views.SchemaEditorView.schema_updated')} "${schemaData?.name}"`
  await loadSchemas()
  await loadVersions(schemaId)
}

async function saveSchema() {
  saveError.value = null
  saveSuccess.value = null

  const valid = await validateSchema()
  if (!valid) return

  saving.value = true
  try {
    const definitionJson = JSON.parse(jsonPreview.value) as Record<string, unknown>

    if (isNew.value) {
      await createSchemaWithVersion(definitionJson)
    } else if (selectedSchemaId.value) {
      await updateSchemaWithVersion(selectedSchemaId.value, definitionJson)
    }
  } catch (e: unknown) {
    saveError.value = `${t('views.SchemaEditorView.save_failed')} ${formatApiError(e)}`
  } finally {
    saving.value = false
  }
}

async function restoreVersion(version: SchemaVersion) {
  schemaVersion.value = version.version
  fields.value = parseDefinitionToFields(version.definition_json)
}

async function copyJsonPreview() {
  try {
    await navigator.clipboard.writeText(jsonPreview.value)
  } catch (e) {
    console.warn('Failed to copy JSON preview', e)
  }
}

watch(() => route.params.id, (newId) => {
  if (newId && typeof newId === 'string') {
    selectSchema(newId)
  }
})

onMounted(() => {
  loadSchemas()
  const id = route.params.id
  if (id && typeof id === 'string') {
    selectSchema(id)
  }
})
</script>
