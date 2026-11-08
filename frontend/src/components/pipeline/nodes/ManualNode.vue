<template>
  <PipelineNodeBase :data="data" :selected="selected" :id="id">
    <template #extra-badges>
      <span v-if="hasOutputSchema" class="inline-flex items-center rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400" data-testid="pipeline-node-output-schema-badge">Schema</span>
    </template>
  </PipelineNodeBase>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import PipelineNodeBase from './PipelineNodeBase.vue'

interface Props { data: Record<string, unknown>; selected?: boolean; id: string }
const props = defineProps<Props>()
const hasOutputSchema = computed(() => Boolean(props.data.output_schema_id))
</script>
