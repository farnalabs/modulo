<template>
  <DataCard
    :title="$t('views.SchemaEditorView.json_preview')"
    heading-level="h2"
    padding="lg"
    elevation="sm"
  >
    <div class="relative">
      <button type="button"
        class="absolute right-2 top-2 rounded p-1 text-muted-foreground hover:bg-accent"
        :aria-label="$t('views.SchemaEditorView.copy_to_clipboard')"
        data-testid="schema-editor-copy-json"
        @click="$emit('copy')"
      >
        <Copy class="h-4 w-4" aria-hidden="true" />
      </button>
      <pre
        class="max-h-80 overflow-x-auto rounded-lg bg-muted p-4 font-mono text-xs leading-relaxed"
        data-testid="schema-editor-json-preview"
      >{{ json }}</pre>
    </div>
  </DataCard>
</template>

<script setup lang="ts">
import DataCard from '../ui/DataCard.vue'
import { Copy } from '@lucide/vue'

defineProps<{
  json: string
}>()

defineEmits<{
  copy: []
}>()
</script>
