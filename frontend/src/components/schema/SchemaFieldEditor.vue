<template>
  <div
    class="rounded-lg border bg-background p-4"
    data-testid="schema-editor-field"
  >
    <div class="flex items-start justify-between gap-2">
      <div class="flex-1 space-y-3">
        <div class="flex items-center gap-2">
          <button type="button"
            class="rounded p-1 text-muted-foreground hover:bg-accent disabled:opacity-30"
            :disabled="isFirst"
            :title="$t('views.SchemaEditorView.move_up')"
            data-testid="schema-editor-field-move-up"
            @click="$emit('move-up')"
          >
            <svg class="h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m18 15-6-6-6 6"/></svg>
          </button>
          <button type="button"
            class="rounded p-1 text-muted-foreground hover:bg-accent disabled:opacity-30"
            :disabled="isLast"
            :title="$t('views.SchemaEditorView.move_down')"
            data-testid="schema-editor-field-move-down"
            @click="$emit('move-down')"
          >
            <svg class="h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>
          </button>
          <span class="text-xs font-medium text-muted-foreground">#{{ index + 1 }}</span>
        </div>

        <div class="grid grid-cols-2 gap-3">
          <div>
            <label :for="`schema-editor-field-${field._key}-name`" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.SchemaEditorView.field_name') }}</label>
            <input :id="`schema-editor-field-${field._key}-name`"
              :value="field.name"
              type="text"
              data-testid="schema-editor-field-name"
              class="w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :placeholder="$t('views.SchemaEditorView.field_name_placeholder')"
              @input="update({ name: ($event.target as HTMLInputElement).value })"
            />
          </div>
          <div>
            <label :for="`schema-editor-field-${field._key}-type`" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.SchemaEditorView.field_type') }}</label>
            <Select
  :aria-label="$t('views.SchemaEditorView.field_type_aria')"
  :model-value="field.type"
  @update:model-value="update({ type: String($event) })"
  :placeholder="$t('views.SchemaEditorView.select_type')"
  data-testid="schema-editor-field-type"
  class="w-full"
  :options="[{ value: 'string', label: 'string' }, { value: 'number', label: 'number' }, { value: 'boolean', label: 'boolean' }, { value: 'array', label: 'array' }, { value: 'object', label: 'object' }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
        </div>

        <div class="grid grid-cols-2 gap-3">
          <div>
            <label :for="`schema-editor-field-${field._key}-description`" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.SchemaEditorView.field_description') }}</label>
            <input :id="`schema-editor-field-${field._key}-description`"
              :value="field.description"
              type="text"
              data-testid="schema-editor-field-description"
              class="w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :placeholder="$t('views.SchemaEditorView.optional')"
              @input="update({ description: ($event.target as HTMLInputElement).value })"
            />
          </div>
          <div>
            <label :for="`schema-editor-field-${field._key}-default`" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.SchemaEditorView.default_value') }}</label>
            <input :id="`schema-editor-field-${field._key}-default`"
              :value="field.defaultValue"
              type="text"
              data-testid="schema-editor-field-default"
              class="w-full rounded-lg border border-input bg-background px-2.5 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              :placeholder="$t('views.SchemaEditorView.optional')"
              @input="update({ defaultValue: ($event.target as HTMLInputElement).value })"
            />
          </div>
        </div>

        <div class="flex items-center gap-4">
          <label :for="`schema-editor-field-${field._key}-required`" class="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input :id="`schema-editor-field-${field._key}-required`"
              :checked="field.required"
              type="checkbox"
              data-testid="schema-editor-field-required"
              class="rounded border-input text-primary focus:ring-primary"
              @change="update({ required: ($event.target as HTMLInputElement).checked })"
            />
            {{ $t('views.SchemaEditorView.required') }}
          </label>
        </div>
      </div>

      <button type="button"
        class="shrink-0 rounded p-1 text-destructive hover:bg-destructive/10"
        data-testid="schema-editor-field-remove"
        :title="$t('views.SchemaEditorView.remove_field')"
        @click="$emit('remove')"
      >
        <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { SchemaField } from '../../utils/schema-definition'
import Select from 'primevue/select'

const field = defineModel<SchemaField>('field', { required: true })

defineProps<{
  index: number
  isFirst: boolean
  isLast: boolean
}>()

defineEmits<{
  'move-up': []
  'move-down': []
  remove: []
}>()

function update(patch: Partial<SchemaField>) {
  field.value = { ...field.value, ...patch }
}
</script>
