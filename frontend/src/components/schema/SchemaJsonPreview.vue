<template>
  <section class="rounded-lg border bg-card p-6 shadow-sm">
    <h2 class="mb-4 text-base font-semibold">{{ $t('views.SchemaEditorView.json_preview') }}</h2>
    <div class="relative">
      <button type="button"
        class="absolute right-2 top-2 rounded p-1 text-muted-foreground hover:bg-accent"
        :title="$t('views.SchemaEditorView.copy_to_clipboard')"
        data-testid="schema-editor-copy-json"
        @click="$emit('copy')"
      >
        <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
      </button>
      <pre
        class="max-h-80 overflow-x-auto rounded-lg bg-muted p-4 font-mono text-xs leading-relaxed"
        data-testid="schema-editor-json-preview"
      >{{ json }}</pre>
    </div>
  </section>
</template>

<script setup lang="ts">
defineProps<{
  json: string
}>()

defineEmits<{
  copy: []
}>()
</script>
