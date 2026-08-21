<template>
  <div class="space-y-6">
    <section class="rounded-lg border bg-card p-6 shadow-sm">
      <h2 class="mb-4 text-base font-semibold">{{ $t('views.SchemaEditorView.schema_details') }}</h2>
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
    </section>

    <section class="rounded-lg border bg-card p-6 shadow-sm">
      <div class="mb-4 flex items-center justify-between">
        <h2 class="text-base font-semibold">{{ $t('views.SchemaEditorView.fields') }}</h2>
        <Button size="small" data-testid="schema-editor-add-field" @click="addField">
          {{ $t('views.SchemaEditorView.add_field') }}
        </Button>
      </div>

      <div v-if="fields.length === 0" class="py-4 text-center text-sm text-muted-foreground">
        {{ $t('views.SchemaEditorView.no_fields') }}
      </div>

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
    </section>
  </div>
</template>

<script setup lang="ts">
import Button from 'primevue/button'
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
