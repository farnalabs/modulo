<template>
  <PipelineNodeBase :data="data" :selected="selected" :id="id" test-id="pipeline-node-router">
    <template #glyph>
      <svg class="h-3 w-3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="6" cy="6" r="2" /><circle cx="18" cy="6" r="2" /><circle cx="12" cy="18" r="2" /><path d="M6 8v3a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V8M12 13v3" /></svg>
      <span>{{ t('views.PipelineEditorView.node_router_badge') }}</span>
    </template>
  </PipelineNodeBase>
</template>

<script setup lang="ts">
import PipelineNodeBase from './PipelineNodeBase.vue'
import { useI18n } from 'vue-i18n'

interface Props { data: Record<string, unknown>; selected?: boolean; id: string }
defineProps<Props>()
const { t } = useI18n()
</script>
