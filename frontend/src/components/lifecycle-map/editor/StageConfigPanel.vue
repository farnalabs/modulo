<template>
  <div class="space-y-4">
    <div>
      <span class="mb-1 block text-sm font-medium">{{ $t('components.lifecycle-map.editor.StageConfigPanel.name') }}</span>
      <InputText v-model="form.name" placeholder="Stage name" />
    </div>

    <div>
      <label for="stageconfigpanel-field-2" class="mb-1 block text-sm font-medium">{{ $t('components.lifecycle-map.editor.StageConfigPanel.description') }}</label>
      <textarea id="stageconfigpanel-field-2"
        v-model="form.description"
        rows="3"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        placeholder="Describe what happens in this stage"
      />
    </div>

    <div>
      <span class="mb-1 block text-sm font-medium">{{ $t('components.lifecycle-map.editor.StageConfigPanel.type') }}</span>
      <div class="grid grid-cols-2 gap-2">
        <button type="button"
          v-for="opt in stageTypeOptions"
          :key="opt.value"
          :class="[
            'rounded-lg border px-3 py-2 text-left text-xs transition-colors',
            form.stage_type === opt.value
              ? 'border-primary bg-primary/10 text-primary'
              : 'border-input bg-background text-muted-foreground hover:border-muted-foreground/50',
          ]"
          @click="form.stage_type = opt.value"
        >
          <div class="font-medium">{{ opt.label }}</div>
          <div class="mt-0.5 text-[10px] opacity-70">{{ opt.description }}</div>
        </button>
      </div>
    </div>

    <div v-if="form.stage_type === 'modulo'">
      <span class="mb-1 block text-sm font-medium">{{ $t('components.lifecycle-map.editor.StageConfigPanel.pipeline') }}</span>
      <Select
  aria-label="Pipeline"
  v-model="form.pipeline_id"
  placeholder="Select a pipeline..."
  class="w-full"
  :options="pipelines.map(p => ({ value: p.id, label: p.name }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
    </div>

    <div v-if="form.stage_type === 'external'">
      <span class="mb-1 block text-sm font-medium">{{ $t('components.lifecycle-map.editor.StageConfigPanel.external_url') }}</span>
      <InputText v-model="form.external_url" placeholder="https://..." />
    </div>

    <div>
      <span class="mb-1 block text-sm font-medium">{{ $t('components.lifecycle-map.editor.StageConfigPanel.owner') }}</span>
      <InputText v-model="form.owner" placeholder="Team or person name" />
    </div>

    <div v-if="isGraduatable" class="border-t pt-4">
      <Button severity="secondary" outlined class="w-full gap-2" @click="$emit('graduate', { ...form, id: stageId })">
        <GraduationCapIcon class="h-4 w-4" />
        Graduate Stage
      </Button>
      <p class="mt-1 text-[10px] text-muted-foreground">
        Promote this {{ form.stage_type }} stage to a Modulo-managed pipeline
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, computed, watch } from 'vue'
import { GraduationCap as GraduationCapIcon } from '@lucide/vue'
import InputText from 'primevue/inputtext'
import Button from 'primevue/button'
import Select from 'primevue/select'
import type { StageType, PipelineSummary } from '../../../types/lifecycleMap'

interface FormModel {
  name: string
  description: string
  stage_type: StageType
  pipeline_id: string | null
  external_url: string
  owner: string
}

const props = defineProps<{
  stageId: string
  name: string
  description: string
  stage_type: StageType
  pipeline_id: string | null
  external_url: string | null
  owner: string | null
  graduated: boolean
  pipelines: PipelineSummary[]
}>()

const emit = defineEmits<{
  update: [field: string, value: unknown]
  graduate: [data: { id: string; name: string; stage_type: StageType }]
}>()

const stageTypeOptions = [
  { value: 'modulo' as StageType, label: 'Modulo', description: 'Managed by a Modulo pipeline' },
  { value: 'external' as StageType, label: 'External', description: 'Runs outside Modulo' },
  { value: 'manual' as StageType, label: 'Manual', description: 'Human-performed step' },
  { value: 'placeholder' as StageType, label: 'Placeholder', description: 'Not yet defined' },
]

const form = reactive<FormModel>({
  name: '',
  description: '',
  stage_type: 'placeholder',
  pipeline_id: null,
  external_url: '',
  owner: '',
})

const isGraduatable = computed(() =>
  !props.graduated && (props.stage_type === 'manual' || props.stage_type === 'external')
)

watch(() => [props.name, props.description, props.stage_type, props.pipeline_id, props.external_url, props.owner], () => {
  form.name = props.name || ''
  form.description = props.description || ''
  form.stage_type = props.stage_type || 'placeholder'
  form.pipeline_id = props.pipeline_id || null
  form.external_url = props.external_url || ''
  form.owner = props.owner || ''
}, { immediate: true })

watch(form, () => {
  emit('update', 'name', form.name)
  emit('update', 'description', form.description)
  emit('update', 'stage_type', form.stage_type)
  emit('update', 'pipeline_id', form.pipeline_id)
  emit('update', 'external_url', form.external_url || null)
  emit('update', 'owner', form.owner || null)
}, { deep: true })
</script>
