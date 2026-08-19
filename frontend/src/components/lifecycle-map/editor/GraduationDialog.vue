<template>
  <Dialog :visible="open" :modal="true" :dismissable-mask="true" class="sm:max-w-lg" @update:visible="$emit('close')">
    <template #header>
      <div>
        <div class="text-lg font-semibold">{{ $t('components.lifecycle-map.editor.GraduationDialog.graduate_stage') }}</div>
        <div class="mt-0.5 text-sm text-muted-foreground">
          {{ $t('components.lifecycle-map.editor.GraduationDialog.graduating_stage_prefix') }} <strong>{{ stageName }}</strong> {{ $t('components.lifecycle-map.editor.GraduationDialog.graduating_stage_suffix') }}
        </div>
      </div>
    </template>

    <div class="space-y-4 py-2">
      <div v-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
        {{ error }}
      </div>

      <div>
        <label for="graduationdialog-field-2" class="mb-2 flex items-center gap-2 text-sm font-medium">
          <input id="graduationdialog-field-2"
            v-model="mode"
            type="radio"
            value="existing"
            class="h-4 w-4 accent-primary"
          />
          Link to existing pipeline
        </label>
        <Select
  aria-label="Select pipeline"
  v-if="mode === 'existing'"
  v-model="selectedPipelineId"
  placeholder="Select a pipeline..."
  class="ml-6 w-full"
  :options="pipelines.map(p => ({ value: p.id, label: p.name }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
      </div>

      <div>
        <label for="graduationdialog-field-1" class="mb-2 flex items-center gap-2 text-sm font-medium">
          <input id="graduationdialog-field-1"
            v-model="mode"
            type="radio"
            value="new"
            class="h-4 w-4 accent-primary"
          />
          Create new pipeline from template
        </label>
        <Select
  aria-label="Select template"
  v-if="mode === 'new'"
  v-model="selectedTemplateId"
  placeholder="Select a template..."
  class="ml-6 w-full"
  :options="[{ value: 'simple-sequential', label: $t('components.lifecycle-map.editor.GraduationDialog.simple_sequential') }, { value: 'hierarchical', label: $t('components.lifecycle-map.editor.GraduationDialog.hierarchical_agent') }, { value: 'parallel', label: $t('components.lifecycle-map.editor.GraduationDialog.parallel_processing') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
      </div>
    </div>

    <template #footer>
      <div class="flex gap-2 justify-end">
        <Button severity="secondary" outlined @click="$emit('close')">{{ $t('common.cancel') }}</Button>
        <Button :disabled="!canGraduate || graduating" @click="handleGraduate">
          {{ graduating ? 'Graduating...' : 'Graduate Stage' }}
        </Button>
      </div>
    </template>
  </Dialog>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import Select from 'primevue/select'
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
