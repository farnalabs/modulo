<template>
  <PipelineNodeBase :data="data" :selected="selected" :id="id" test-id="pipeline-node-router" />
</template>

<script setup lang="ts">
import PipelineNodeBase from './PipelineNodeBase.vue'

interface Props { data: Record<string, unknown>; selected?: boolean; id: string }
defineProps<Props>()
</script>
