<template>
  <PipelineNodeBase :data="data" :selected="selected" :id="id">
    <template #extra-badges>
      <span v-if="hasCapabilityScope" class="inline-flex items-center rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-600 dark:bg-indigo-900 dark:text-indigo-300" data-testid="pipeline-node-scope-badge">{{ t('views.PipelineEditorView.capability_scope_scoped_badge') }}</span>
    </template>
  </PipelineNodeBase>
</template>

<script setup lang="ts">
import PipelineNodeBase from './PipelineNodeBase.vue'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

interface Props { data: Record<string, unknown>; selected?: boolean; id: string }
const props = defineProps<Props>()
const { t } = useI18n()
const hasCapabilityScope = computed(() => Boolean(props.data.hasCapabilityScope))
</script>
