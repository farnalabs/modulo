<template>
  <Dialog :open="open" @update:open="$emit('close')">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>Graduate Stage</DialogTitle>
        <DialogDescription>
          You are graduating <strong>{{ stageName }}</strong> to a Modulo-managed pipeline.
        </DialogDescription>
      </DialogHeader>

      <div class="space-y-4 py-2">
        <div v-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          {{ error }}
        </div>

        <div>
          <label class="mb-2 flex items-center gap-2 text-sm font-medium">
            <input
              v-model="mode"
              type="radio"
              value="existing"
              class="h-4 w-4 accent-primary"
            />
            Link to existing pipeline
          </label>
          <select
            v-if="mode === 'existing'"
            v-model="selectedPipelineId"
            class="ml-6 w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <option :value="null">Select a pipeline...</option>
            <option v-for="p in pipelines" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
        </div>

        <div>
          <label class="mb-2 flex items-center gap-2 text-sm font-medium">
            <input
              v-model="mode"
              type="radio"
              value="new"
              class="h-4 w-4 accent-primary"
            />
            Create new pipeline from template
          </label>
          <select
            v-if="mode === 'new'"
            v-model="selectedTemplateId"
            class="ml-6 w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <option :value="null">Select a template...</option>
            <option value="simple-sequential">Simple Sequential</option>
            <option value="hierarchical">Hierarchical Agent</option>
            <option value="parallel">Parallel Processing</option>
          </select>
        </div>
      </div>

      <DialogFooter>
        <Button variant="outline" @click="$emit('close')">Cancel</Button>
        <Button
          :disabled="!canGraduate || graduating"
          @click="handleGraduate"
        >
          {{ graduating ? 'Graduating...' : 'Graduate Stage' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import type { PipelineSummary } from '../../../types/lifecycleMap'

const props = defineProps<{
  open: boolean
  stageName: string
  stageId: string
  mapId: string
  versionId: string
  pipelines: PipelineSummary[]
}>()

const emit = defineEmits<{
  close: []
  confirm: [stageId: string, pipelineId: string]
}>()

const mode = ref<'existing' | 'new'>('existing')
const selectedPipelineId = ref<string | null>(null)
const selectedTemplateId = ref<string | null>(null)
const graduating = ref(false)
const error = ref<string | null>(null)

const canGraduate = computed(() => {
  if (mode.value === 'existing') return !!selectedPipelineId.value
  return !!selectedTemplateId.value
})

async function handleGraduate() {
  if (!canGraduate.value) return
  graduating.value = true
  error.value = null
  try {
    if (mode.value === 'existing' && selectedPipelineId.value) {
      emit('confirm', props.stageId, selectedPipelineId.value)
    } else if (mode.value === 'new') {
      emit('confirm', props.stageId, 'new-from-template')
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to graduate stage'
  } finally {
    graduating.value = false
  }
}
</script>
