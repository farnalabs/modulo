<template>
  <div class="space-y-6">
    <DataCard
      :title="$t('views.SchemaEditorView.schema_details')"
      heading-level="h2"
      padding="lg"
    >
      <div class="space-y-4">
        <div>
          <label for="schemaeditorview-field-8" class="mb-1 block text-sm font-medium">{{ $t('views.SchemaEditorView.name') }}</label>
          <input id="schemaeditorview-field-8"
            v-model="name"
            type="text"
            data-testid="schema-editor-name"
            class="input-base"
            :placeholder="$t('views.SchemaEditorView.name_placeholder')"
          />
        </div>
        <div>
          <label for="schemaeditorview-field-7" class="mb-1 block text-sm font-medium">{{ $t('views.SchemaEditorView.description') }}</label>
          <input id="schemaeditorview-field-7"
            v-model="description"
            type="text"
            data-testid="schema-editor-description"
            class="input-base"
            :placeholder="$t('views.SchemaEditorView.description_placeholder')"
          />
        </div>
        <div>
          <label for="schemaeditorview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.SchemaEditorView.version') }}</label>
          <input id="schemaeditorview-field-6"
            v-model="version"
            type="text"
            data-testid="schema-editor-version"
            class="input-base"
            :placeholder="$t('views.SchemaEditorView.version_placeholder')"
          />
        </div>
      </div>
    </DataCard>

    <DataCard
      :title="$t('views.SchemaEditorView.fields')"
      heading-level="h2"
      padding="lg"
    >
      <template #headerActions>
        <Button size="small" data-testid="schema-editor-add-field" @click="addField">
          {{ $t('views.SchemaEditorView.add_field') }}
        </Button>
      </template>

      <EmptyState v-if="fields.length === 0" :title="$t('views.SchemaEditorView.no_fields')" />

      <div class="space-y-3">
        <SchemaFieldEditor
          v-for="(field, index) in fields"
          :key="field._key"
          :field="field"
          :index="index"
          :is-first="index === 0"
          :is-last="index === fields.length - 1"
          @update:field="updateField(index, $event)"
          @move-up="moveField(index, -1)"
          @move-down="moveField(index, 1)"
          @remove="removeField(index)"
        />
      </div>
    </DataCard>
  </div>
</template>

<script setup lang="ts">
import DataCard from '../ui/DataCard.vue'
import Button from 'primevue/button'
import EmptyState from '../shared/EmptyState.vue'
import { createField, type SchemaField } from '../../utils/schema-definition'
import SchemaFieldEditor from './SchemaFieldEditor.vue'

const name = defineModel<string>('name', { required: true })
const description = defineModel<string>('description', { required: true })
const version = defineModel<string>('version', { required: true })
const fields = defineModel<SchemaField[]>('fields', { required: true })

function addField() {
  const nextKey = fields.value.reduce((max, f) => Math.max(max, f._key), 0) + 1
  fields.value = [...fields.value, createField(nextKey)]
}

function removeField(index: number) {
  fields.value = fields.value.filter((_, i) => i !== index)
}

function moveField(index: number, delta: number) {
  const newIndex = index + delta
  if (newIndex < 0 || newIndex >= fields.value.length) return
  const next = [...fields.value]
  const [item] = next.splice(index, 1)
  next.splice(newIndex, 0, item)
  fields.value = next
}

function updateField(index: number, updated: SchemaField) {
  fields.value = fields.value.map((f, i) => (i === index ? updated : f))
}
</script>
