<template>
  <PipelineNodeBase :data="data" :selected="selected" :id="id">
    <template #extra-badges>
      <span v-if="hasCapabilityScope" class="inline-flex items-center rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-600 dark:bg-indigo-900 dark:text-indigo-300" data-testid="pipeline-node-scope-badge">Scoped</span>
    </template>
  </PipelineNodeBase>
</template>

<script setup lang="ts">
import PipelineNodeBase from './PipelineNodeBase.vue'
import { computed } from 'vue'

interface Props { data: Record<string, unknown>; selected?: boolean; id: string }
const props = defineProps<Props>()
const hasCapabilityScope = computed(() => props.data.hasCapabilityScope === true)
</script>
