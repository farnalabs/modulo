<template>
  <PageTabs :tabs="[
    { label: $t('views.SchemaInferenceView.browse'), to: '/schemas' },
    { label: $t('views.SchemaInferenceView.editor'), to: '/schemas/editor' },
    { label: $t('views.SchemaInferenceView.infer'), to: '/schemas/infer' },
  ]" />
  <div class="flex h-[calc(100vh-3.5rem)]">
    <SchemaEditorSidebar
      :schemas="filteredSchemas"
      :loading="loadingSchemas"
      :selected-id="selectedSchemaId"
      :search-query="searchQuery"
      @select="selectSchema"
      @create="createNewSchema"
      @update:search-query="searchQuery = $event"
    />

    <main class="flex-1 overflow-y-auto">
      <div v-if="!editingSchema" class="flex h-full items-center justify-center text-sm text-muted-foreground">
        {{ $t('views.SchemaEditorView.select_or_create') }}
      </div>

      <template v-else>
        <div class="space-y-6 p-6">
          <header class="flex items-center justify-between">
            <PageHeader :title="isNew ? $t('views.SchemaEditorView.new_schema_title') : $t('views.SchemaEditorView.edit_schema_title')" :subtitle="isNew ? $t('views.SchemaEditorView.define_new_schema') : schemaName" />
            <div class="flex items-center gap-2">
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
            </div>
          </header>

          <div v-if="validationErrors.length > 0" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
            <p class="mb-2 text-sm font-medium text-destructive">{{ $t('views.SchemaEditorView.validation_errors') }}</p>
            <ul class="list-inside list-disc space-y-1 text-sm text-destructive/90">
              <li v-for="err in validationErrors" :key="err">{{ err }}</li>
            </ul>
          </div>

          <div v-if="saveError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
            {{ saveError }}
          </div>

          <div v-if="saveSuccess" class="rounded-lg border border-success/50 bg-success/10 p-4 text-sm text-success">
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
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import { buildJsonSchema, parseDefinitionToFields, type SchemaField } from '../utils/schema-definition'
import FeatureGate from '../components/FeatureGate.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import PageTabs from "../components/PageTabs.vue"
import Button from 'primevue/button'
import SchemaEditorSidebar, { type SchemaItem } from '../components/schema/SchemaEditorSidebar.vue'
import SchemaEditorForm from '../components/schema/SchemaEditorForm.vue'
import SchemaVersionHistory, { type SchemaVersion } from '../components/schema/SchemaVersionHistory.vue'
import SchemaJsonPreview from '../components/schema/SchemaJsonPreview.vue'

const { t } = useI18n()

const route = useRoute()

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

async function validateSchema(): Promise<boolean> {
  validationErrors.value = []
  const errors: string[] = []

  if (!schemaName.value.trim()) errors.push(t('views.SchemaEditorView.schema_name_required'))
  if (!schemaVersion.value.trim()) errors.push(t('views.SchemaEditorView.schema_version_required'))
  if (fields.value.length === 0) errors.push(t('views.SchemaEditorView.at_least_one_field'))

  const seen = new Set<string>()
  for (const field of fields.value) {
    if (!field.name.trim()) {
      errors.push(t('views.SchemaEditorView.all_fields_must_have_name'))
      break
    }
    if (seen.has(field.name.trim())) {
      errors.push(`${t('views.SchemaEditorView.duplicate_field_name')} "${field.name.trim()}"`)
    }
    seen.add(field.name.trim())
  }

  if (errors.length > 0) {
    validationErrors.value = errors
    return false
  }

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

  if (errors.length > 0) {
    validationErrors.value = errors
    return false
  }
  return true
}

async function saveSchema() {
  saveError.value = null
  saveSuccess.value = null

  const valid = await validateSchema()
  if (!valid) return

  saving.value = true
  try {
    const definitionJson = JSON.parse(jsonPreview.value)

    if (isNew.value) {
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
    } else if (selectedSchemaId.value) {
      const { data: schemaData, error: updateErr } = await api.PATCH('/api/v1/schemas/{schema_id}', {
        params: { path: { schema_id: selectedSchemaId.value } },
        body: {
          name: schemaName.value.trim(),
          description: schemaDescription.value.trim() || null,
        },
      })
      if (updateErr) {
        saveError.value = `${t('views.SchemaEditorView.update_failed')} ${formatApiError(updateErr)}`
        return
      }

      const nextVersion = versions.value.length > 0
        ? Math.max(...versions.value.map(v => v.version_number)) + 1
        : 1
      const { error: versionErr } = await api.POST('/api/v1/schemas/{schema_id}/versions', {
        params: { path: { schema_id: selectedSchemaId.value } },
        body: {
          version: schemaVersion.value.trim(),
          version_number: nextVersion,
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
      await loadVersions(selectedSchemaId.value)
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
