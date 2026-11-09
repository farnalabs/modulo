<template>
  <PipelineNodeBase :data="data" :selected="selected" :id="id" test-id="pipeline-node-hitl">
    <template #glyph>
      <svg class="h-3 w-3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 10-4-2.5-7-5.5-7-10V6z" /><path d="M9 12l2 2 4-4" /></svg>
      <span>{{ t('views.PipelineEditorView.node_hitl_badge') }}</span>
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
