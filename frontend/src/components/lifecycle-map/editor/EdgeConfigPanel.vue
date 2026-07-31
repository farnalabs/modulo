<template>
  <div class="space-y-4">
    <div>
      <label for="edgeconfigpanel-field-5" class="mb-1 block text-sm font-medium">{{ $t('components.lifecycle-map.editor.EdgeConfigPanel.trigger_type') }}</label>
      <Select v-model="form.trigger_type">
        <SelectTrigger class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label="Trigger Type">
          <SelectValue :placeholder="$t('components.lifecycle-map.editor.EdgeConfigPanel.select_trigger_type')" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem v-for="opt in triggerOptions" :key="opt.value" :value="opt.value">
            {{ opt.label }}
          </SelectItem>
        </SelectContent>
      </Select>
    </div>

    <div>
      <label for="edgeconfigpanel-field-4" class="mb-1 block text-sm font-medium">{{ $t('components.lifecycle-map.editor.EdgeConfigPanel.description') }}</label>
      <textarea id="edgeconfigpanel-field-4"
        v-model="form.description"
        rows="2"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        placeholder="Describe the trigger condition"
      />
    </div>

    <div>
      <label for="edgeconfigpanel-field-3" class="mb-1 block text-sm font-medium">{{ $t('components.lifecycle-map.editor.EdgeConfigPanel.condition_expression_jmespath') }}</label>
      <input id="edgeconfigpanel-field-3"
        v-model="form.condition_expression"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        placeholder="e.g. result.status == 'success'"
      />
    </div>

    <div>
      <label for="edgeconfigpanel-field-2" class="mb-1 block text-sm font-medium">{{ $t('components.lifecycle-map.editor.EdgeConfigPanel.estimated_frequency') }}</label>
      <Select v-model="form.estimated_frequency">
        <SelectTrigger class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label="Estimated Frequency">
          <SelectValue :placeholder="$t('components.lifecycle-map.editor.EdgeConfigPanel.not_specified')" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="daily">{{ $t('components.lifecycle-map.editor.EdgeConfigPanel.daily') }}</SelectItem>
          <SelectItem value="per-pr">{{ $t('components.lifecycle-map.editor.EdgeConfigPanel.per_pr') }}</SelectItem>
          <SelectItem value="hourly">{{ $t('components.lifecycle-map.editor.EdgeConfigPanel.hourly') }}</SelectItem>
          <SelectItem value="custom">{{ $t('components.lifecycle-map.editor.EdgeConfigPanel.custom') }}</SelectItem>
        </SelectContent>
      </Select>
    </div>

    <div>
      <label for="edgeconfigpanel-field-1" class="mb-1 block text-sm font-medium">{{ $t('components.lifecycle-map.editor.EdgeConfigPanel.trigger_link_optional') }}</label>
      <input id="edgeconfigpanel-field-1"
        v-model="form.trigger_link"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        placeholder="Link to Modulo trigger config"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, watch } from 'vue'
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '@/components/ui/select'
import type { TriggerType, EstimatedFrequency } from '../../../types/lifecycleMap'

interface FormModel {
  trigger_type: TriggerType
  description: string
  condition_expression: string
  estimated_frequency: EstimatedFrequency | null
  trigger_link: string
}

const props = defineProps<{
  trigger_type: TriggerType
  description: string
  condition_expression: string | null
  estimated_frequency: EstimatedFrequency | null
  trigger_link: string | null
}>()

const emit = defineEmits<{
  update: [field: string, value: unknown]
}>()

const triggerOptions = [
  { value: 'pipeline_completed' as TriggerType, label: 'Pipeline Completed' },
  { value: 'webhook' as TriggerType, label: 'Webhook' },
  { value: 'cron' as TriggerType, label: 'Cron / Schedule' },
  { value: 'manual' as TriggerType, label: 'Manual' },
  { value: 'external' as TriggerType, label: 'External' },
]

const form = reactive<FormModel>({
  trigger_type: 'pipeline_completed',
  description: '',
  condition_expression: '',
  estimated_frequency: null,
  trigger_link: '',
})

watch(() => [props.trigger_type, props.description, props.condition_expression, props.estimated_frequency, props.trigger_link], () => {
  form.trigger_type = props.trigger_type || 'pipeline_completed'
  form.description = props.description || ''
  form.condition_expression = props.condition_expression || ''
  form.estimated_frequency = props.estimated_frequency || null
  form.trigger_link = props.trigger_link || ''
}, { immediate: true })

watch(form, () => {
  emit('update', 'trigger_type', form.trigger_type)
  emit('update', 'description', form.description)
  emit('update', 'condition_expression', form.condition_expression || null)
  emit('update', 'estimated_frequency', form.estimated_frequency)
  emit('update', 'trigger_link', form.trigger_link || null)
}, { deep: true })
</script>
