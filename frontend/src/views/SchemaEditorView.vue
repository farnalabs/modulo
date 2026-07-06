<template>
  <PageTabs :tabs="[
    { label: 'Browse', to: '/schemas' },
    { label: 'Editor', to: '/schemas/editor' },
    { label: 'Infer', to: '/schemas/infer' },
  ]" />
  <div class="flex h-[calc(100vh-3.5rem)]">
    <aside class="flex w-80 flex-col border-r bg-background">
      <div class="border-b p-4">
        <h2 class="text-lg font-semibold">{{ $t('views.SchemaEditorView.schemas') }}</h2>
        <div class="relative mt-2">
          <input
            v-model="searchQuery"
            type="text"
            :placeholder="$t('views.SchemaEditorView.search_schemas')"
            data-testid="schema-editor-search"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 pl-9 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <svg class="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
        </div>
      </div>

      <div class="flex-1 overflow-y-auto">
        <LoadingSpinner v-if="loadingSchemas" />
        <div v-else-if="schemas.length === 0" class="p-4 text-center text-sm text-muted-foreground">
          {{ $t('views.SchemaEditorView.no_schemas_yet') }}
        </div>
        <template v-else>
          <div
            v-for="schema in filteredSchemas"
            :key="schema.id"
            class="cursor-pointer border-b px-4 py-3 transition-colors hover:bg-muted/50"
            :class="{ 'bg-muted': selectedSchemaId === schema.id }"
            data-testid="schema-editor-list-item"
            @click="selectSchema(schema.id)"
          >
            <div class="flex items-center justify-between">
              <span class="text-sm font-medium">{{ schema.name }}</span>
              <span
                v-if="schema.deprecated"
                class="rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] font-medium text-destructive"
              >Deprecated</span>
            </div>
            <p v-if="schema.description" class="mt-0.5 truncate text-xs text-muted-foreground">{{ schema.description }}</p>
          </div>
        </template>
      </div>

      <div class="border-t p-4">
        <button
          data-testid="schema-editor-new"
          class="w-full rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          @click="createNewSchema"
        >
          {{ $t('views.SchemaEditorView.new_schema') }}
        </button>
      </div>
    </aside>

    <main class="flex-1 overflow-y-auto">
      <div v-if="!editingSchema" class="flex h-full items-center justify-center text-sm text-muted-foreground">
        {{ $t('views.SchemaEditorView.select_or_create') }}
      </div>

      <template v-else>
        <div class="space-y-6 p-6">
          <header class="flex items-center justify-between">
            <div>
              <h1 class="text-2xl font-bold tracking-tight">{{ isNew ? $t('views.SchemaEditorView.new_schema_title') : $t('views.SchemaEditorView.edit_schema_title') }}</h1>
              <p class="mt-0.5 text-sm text-muted-foreground">{{ isNew ? $t('views.SchemaEditorView.define_new_schema') : schemaName }}</p>
            </div>
            <div class="flex items-center gap-2">
              <button
                data-testid="schema-editor-save"
                :disabled="saving || !isValid"
                class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                @click="saveSchema"
              >
                {{ saving ? $t('views.SchemaEditorView.saving') : $t('views.SchemaEditorView.save') }}
              </button>
              <button
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
            <div class="space-y-6">
              <section class="rounded-lg border bg-card p-6 shadow-sm">
                <h2 class="mb-4 text-lg font-semibold">{{ $t('views.SchemaEditorView.schema_details') }}</h2>
                <div class="space-y-4">
                  <div>
                    <label class="mb-1 block text-sm font-medium">{{ $t('views.SchemaEditorView.name') }}</label>
                    <input
                      v-model="schemaName"
                      type="text"
                      data-testid="schema-editor-name"
                      class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      placeholder="My Schema"
                    />
                  </div>
                  <div>
                    <label class="mb-1 block text-sm font-medium">{{ $t('views.SchemaEditorView.description') }}</label>
                    <input
                      v-model="schemaDescription"
                      type="text"
                      data-testid="schema-editor-description"
                      class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      placeholder="Optional description"
                    />
                  </div>
                  <div>
                    <label class="mb-1 block text-sm font-medium">{{ $t('views.SchemaEditorView.version') }}</label>
                    <input
                      v-model="schemaVersion"
                      type="text"
                      data-testid="schema-editor-version"
                      class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      placeholder="1.0.0"
                    />
                  </div>
                </div>
              </section>

              <section class="rounded-lg border bg-card p-6 shadow-sm">
                <div class="mb-4 flex items-center justify-between">
                  <h2 class="text-lg font-semibold">{{ $t('views.SchemaEditorView.fields') }}</h2>
                  <button
                    data-testid="schema-editor-add-field"
                    class="rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90"
                    @click="addField"
                  >
                    {{ $t('views.SchemaEditorView.add_field') }}
                  </button>
                </div>

                <div v-if="fields.length === 0" class="py-4 text-center text-sm text-muted-foreground">
                  {{ $t('views.SchemaEditorView.no_fields') }}
                </div>

                <div class="space-y-3">
                  <div
                    v-for="(field, index) in fields"
                    :key="field._key"
                    class="rounded-lg border bg-background p-4"
                    data-testid="schema-editor-field"
                  >
                    <div class="flex items-start justify-between gap-2">
                      <div class="flex-1 space-y-3">
                        <div class="flex items-center gap-2">
                          <button
                            class="rounded p-1 text-muted-foreground hover:bg-accent disabled:opacity-30"
                            :disabled="index === 0"
                            :title="$t('views.SchemaEditorView.move_up')"
                            data-testid="schema-editor-field-move-up"
                            @click="moveField(index, -1)"
                          >
                            <svg class="h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m18 15-6-6-6 6"/></svg>
                          </button>
                          <button
                            class="rounded p-1 text-muted-foreground hover:bg-accent disabled:opacity-30"
                            :disabled="index === fields.length - 1"
                            :title="$t('views.SchemaEditorView.move_down')"
                            data-testid="schema-editor-field-move-down"
                            @click="moveField(index, 1)"
                          >
                            <svg class="h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>
                          </button>
                          <span class="text-xs font-medium text-muted-foreground">#{{ index + 1 }}</span>
                        </div>

                        <div class="grid grid-cols-2 gap-3">
                          <div>
                            <label class="mb-1 block text-xs text-muted-foreground">{{ $t('views.SchemaEditorView.field_name') }}</label>
                            <input
                              v-model="field.name"
                              type="text"
                              data-testid="schema-editor-field-name"
                              class="w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              placeholder="field_name"
                            />
                          </div>
                          <div>
                            <label class="mb-1 block text-xs text-muted-foreground">{{ $t('views.SchemaEditorView.field_type') }}</label>
                            <select
                              v-model="field.type"
                              data-testid="schema-editor-field-type"
                              class="w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            >
                              <option value="string">string</option>
                              <option value="number">number</option>
                              <option value="boolean">boolean</option>
                              <option value="array">array</option>
                              <option value="object">object</option>
                            </select>
                          </div>
                        </div>

                        <div class="grid grid-cols-2 gap-3">
                          <div>
                            <label class="mb-1 block text-xs text-muted-foreground">{{ $t('views.SchemaEditorView.field_description') }}</label>
                            <input
                              v-model="field.description"
                              type="text"
                              data-testid="schema-editor-field-description"
                              class="w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              placeholder="Optional"
                            />
                          </div>
                          <div>
                            <label class="mb-1 block text-xs text-muted-foreground">{{ $t('views.SchemaEditorView.default_value') }}</label>
                            <input
                              v-model="field.defaultValue"
                              type="text"
                              data-testid="schema-editor-field-default"
                              class="w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              placeholder="Optional"
                            />
                          </div>
                        </div>

                        <div class="flex items-center gap-4">
                          <label class="flex items-center gap-1.5 text-xs text-muted-foreground">
                            <input
                              v-model="field.required"
                              type="checkbox"
                              data-testid="schema-editor-field-required"
                              class="rounded border-input text-primary focus:ring-primary"
                            />
                            {{ $t('views.SchemaEditorView.required') }}
                          </label>
                        </div>
                      </div>

                      <button
                        class="shrink-0 rounded p-1 text-destructive hover:bg-destructive/10"
                        data-testid="schema-editor-field-remove"
                        :title="$t('views.SchemaEditorView.remove_field')"
                        @click="removeField(index)"
                      >
                        <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
                      </button>
                    </div>
                  </div>
                </div>
              </section>
            </div>

            <div class="space-y-6">
              <FeatureGate feature-name="schema_version_history" required-tier="team" show-disabled>
                <section class="rounded-lg border bg-card p-6 shadow-sm">
                  <h2 class="mb-4 text-lg font-semibold">{{ $t('views.SchemaEditorView.version_history') }}</h2>
                  <LoadingSpinner v-if="loadingVersions" />
                  <p v-else-if="versions.length === 0" class="text-sm text-muted-foreground">{{ $t('views.SchemaEditorView.no_version_history') }}</p>
                  <div v-else class="space-y-2">
                    <div
                      v-for="version in versions"
                      :key="version.id"
                      class="flex items-center justify-between rounded-lg border bg-background px-3 py-2"
                    >
                      <div class="flex items-center gap-2">
                        <span class="text-sm font-medium">v{{ version.version }}</span>
                        <span
                          v-if="version.published"
                          class="rounded bg-success/10 px-1.5 py-0.5 text-[10px] font-medium text-success"
                        >{{ $t('views.SchemaEditorView.published') }}</span>
                        <span class="text-xs text-muted-foreground">{{ formatDate(version.created_at) }}</span>
                      </div>
                      <button
                        data-testid="schema-editor-restore-version"
                        class="rounded px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10"
                        @click="restoreVersion(version)"
                      >
                        {{ $t('views.SchemaEditorView.restore') }}
                      </button>
                    </div>
                  </div>
                </section>
              </FeatureGate>

              <section class="rounded-lg border bg-card p-6 shadow-sm">
                <h2 class="mb-4 text-lg font-semibold">{{ $t('views.SchemaEditorView.json_preview') }}</h2>
                <div class="relative">
                  <button
                    class="absolute right-2 top-2 rounded p-1 text-muted-foreground hover:bg-accent"
                    :title="$t('views.SchemaEditorView.copy_to_clipboard')"
                    data-testid="schema-editor-copy-json"
                    @click="copyJsonPreview"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                  </button>
                  <pre
                    class="max-h-80 overflow-x-auto rounded-lg bg-muted p-4 font-mono text-xs leading-relaxed"
                    data-testid="schema-editor-json-preview"
                  >{{ jsonPreview }}</pre>
                </div>
              </section>
            </div>
          </div>
        </div>
      </template>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api, getAccessToken } from '../lib/api/client'
import { formatApiError, type ProblemDetail } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import FeatureGate from '../components/FeatureGate.vue'
import PageTabs from "../components/PageTabs.vue"

const { t } = useI18n()

type SchemaItem = components['schemas']['SchemaItem']

interface SchemaField {
  _key: number
  name: string
  type: string
  required: boolean
  description: string
  defaultValue: string
}

interface SchemaVersion {
  id: string
  schema_id: string
  version: string
  version_number: number
  definition_json: Record<string, unknown>
  published: boolean
  created_at: string
}

let fieldKeyCounter = 0

const route = useRoute()
const router = useRouter()

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

function createField(): SchemaField {
  return {
    _key: ++fieldKeyCounter,
    name: '',
    type: 'string',
    required: false,
    description: '',
    defaultValue: '',
  }
}

const isValid = computed(() => {
  if (!schemaName.value.trim()) return false
  if (!schemaVersion.value.trim()) return false
  if (fields.value.length === 0) return false
  return fields.value.every(f => f.name.trim())
})

const jsonPreview = computed(() => {
  const properties: Record<string, unknown> = {}
  const requiredFields: string[] = []

  for (const field of fields.value) {
    if (!field.name.trim()) continue
    const prop: Record<string, unknown> = { type: field.type }
    if (field.description) prop.description = field.description
    if (field.defaultValue) {
      prop.default = coerceDefault(field.defaultValue, field.type)
    }
    properties[field.name.trim()] = prop
    if (field.required) requiredFields.push(field.name.trim())
  }

  const schema: Record<string, unknown> = {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    title: schemaName.value || 'Untitled Schema',
    type: 'object',
    properties,
  }
  if (schemaDescription.value) schema.description = schemaDescription.value
  if (requiredFields.length > 0) schema.required = requiredFields

  return JSON.stringify(schema, null, 2)
})

function coerceDefault(value: string, type: string): unknown {
  switch (type) {
    case 'number': {
      const n = Number(value)
      return Number.isNaN(n) ? value : n
    }
    case 'boolean': {
      if (value === 'true') return true
      if (value === 'false') return false
      return value
    }
    default:
      return value
  }
}

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
    saveError.value = `${t('views.SchemaEditorView.failed_to_load_schemas')} ${e instanceof Error ? e.message : String(e)}`
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

function addField() {
  fields.value.push(createField())
}

function removeField(index: number) {
  fields.value.splice(index, 1)
}

function moveField(index: number, delta: number) {
  const newIndex = index + delta
  if (newIndex < 0 || newIndex >= fields.value.length) return
  const temp = fields.value[index]
  fields.value[index] = fields.value[newIndex]
  fields.value[newIndex] = temp
}

async function loadLatestVersion(schemaId: string) {
  try {
    const token = getAccessToken()
    const res = await fetch(`/api/v1/schemas/${schemaId}/versions?page=1&page_size=1`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!res.ok) return
    const data = await res.json()
    if (data.items && data.items.length > 0) {
      const latest = data.items[0]
      schemaVersion.value = latest.version
      const def = latest.definition_json
      if (def.properties && typeof def.properties === 'object') {
        const loadedFields: SchemaField[] = []
        for (const [name, prop] of Object.entries(def.properties as Record<string, any>)) {
          loadedFields.push({
            _key: ++fieldKeyCounter,
            name,
            type: prop.type ?? 'string',
            required: Array.isArray(def.required) && def.required.includes(name),
            description: prop.description ?? '',
            defaultValue: prop.default !== undefined ? String(prop.default) : '',
          })
        }
        fields.value = loadedFields
      }
    }
  } catch (err) {
    console.warn('Failed to load schema fields:', err)
  }
}

async function loadVersions(schemaId: string) {
  loadingVersions.value = true
  try {
    const token = getAccessToken()
    const res = await fetch(`/api/v1/schemas/${schemaId}/versions?page=1&page_size=50`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!res.ok) return
    const data = await res.json()
    versions.value = data.items ?? []
  } catch {
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
    const token = getAccessToken()
    const res = await fetch('/api/v1/schemas/validate', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ definition: JSON.parse(jsonPreview.value) }),
    })
    const result = await res.json()
    if (!result.valid && result.errors) {
      for (const e of result.errors) {
        errors.push(`${e.path}: ${e.message}`)
      }
    }
  } catch (err) {
    console.warn('Server-side validation unavailable:', err)
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

      const token = getAccessToken()
      const versionRes = await fetch(`/api/v1/schemas/${schemaData.id}/versions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          version: schemaVersion.value.trim(),
          version_number: 1,
          definition_json: definitionJson,
          published: true,
        }),
      })
      if (!versionRes.ok) {
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

      const token = getAccessToken()
      const nextVersion = versions.value.length > 0
        ? Math.max(...versions.value.map(v => v.version_number)) + 1
        : 1
      const versionRes = await fetch(`/api/v1/schemas/${selectedSchemaId.value}/versions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          version: schemaVersion.value.trim(),
          version_number: nextVersion,
          definition_json: definitionJson,
          published: true,
        }),
      })
      if (!versionRes.ok) {
        saveError.value = t('views.SchemaEditorView.schema_updated_version_failed')
        return
      }

      saveSuccess.value = `${t('views.SchemaEditorView.schema_updated')} "${schemaData?.name}"`
      await loadSchemas()
      await loadVersions(selectedSchemaId.value)
    }
  } catch (e: unknown) {
    saveError.value = `${t('views.SchemaEditorView.save_failed')} ${e instanceof Error ? e.message : String(e)}`
  } finally {
    saving.value = false
  }
}

async function restoreVersion(version: SchemaVersion) {
  const def = version.definition_json
  schemaVersion.value = version.version
  if (def.properties && typeof def.properties === 'object') {
    const loadedFields: SchemaField[] = []
    for (const [name, prop] of Object.entries(def.properties as Record<string, any>)) {
      loadedFields.push({
        _key: ++fieldKeyCounter,
        name,
        type: prop.type ?? 'string',
        required: Array.isArray(def.required) && def.required.includes(name),
        description: prop.description ?? '',
        defaultValue: prop.default !== undefined ? String(prop.default) : '',
      })
    }
    fields.value = loadedFields
  }
}

function formatDate(dateStr: string): string {
  try {
    return new Date(dateStr).toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
    })
  } catch {
    return dateStr
  }
}

async function copyJsonPreview() {
  try {
    await navigator.clipboard.writeText(jsonPreview.value)
  } catch (err) {
    console.warn('Clipboard write failed:', err)
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
