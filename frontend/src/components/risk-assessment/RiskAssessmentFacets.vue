<template>
  <div class="card p-4 space-y-4" data-testid="risk-assessment-facets">
    <h3 class="text-base font-semibold">{{ $t('views.RiskAssessmentView.facets_title') }}</h3>

    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <!-- Key Prefix Filter -->
      <div>
        <label for="risk-assessment-key-prefix" class="mb-1 block text-sm font-medium">
          {{ $t('views.RiskAssessmentView.key_prefix_label') }}
        </label>
        <input
          id="risk-assessment-key-prefix"
          v-model="localFilters.keyPrefix"
          type="text"
          placeholder="risk."
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          data-testid="risk-assessment-key-prefix"
        />
      </div>

      <!-- Pipeline Filter -->
      <div>
        <label for="risk-assessment-pipeline" class="mb-1 block text-sm font-medium">
          {{ $t('views.RiskAssessmentView.pipeline_label') }}
        </label>
        <select
          id="risk-assessment-pipeline"
          v-model="localFilters.pipelineId"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          data-testid="risk-assessment-pipeline"
        >
          <option value="">{{ $t('views.RiskAssessmentView.all_pipelines') }}</option>
          <option
            v-for="pipeline in pipelines"
            :key="pipeline.id"
            :value="pipeline.id"
          >
            {{ pipeline.name }}
          </option>
        </select>
      </div>

      <!-- Time Window Filter -->
      <div>
        <label for="risk-assessment-days" class="mb-1 block text-sm font-medium">
          {{ $t('views.RiskAssessmentView.time_window_label') }}
        </label>
        <select
          id="risk-assessment-days"
          v-model="localFilters.days"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          data-testid="risk-assessment-days"
        >
          <option :value="1">{{ $t('views.RiskAssessmentView.last_1_day') }}</option>
          <option :value="7">{{ $t('views.RiskAssessmentView.last_7_days') }}</option>
          <option :value="30">{{ $t('views.RiskAssessmentView.last_30_days') }}</option>
          <option :value="90">{{ $t('views.RiskAssessmentView.last_90_days') }}</option>
        </select>
      </div>

      <!-- Action Filter -->
      <div>
        <label for="risk-assessment-action" class="mb-1 block text-sm font-medium">
          {{ $t('views.RiskAssessmentView.action_label') }}
        </label>
        <select
          id="risk-assessment-action"
          v-model="localFilters.action"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          :disabled="facetDegraded"
          data-testid="risk-assessment-action"
        >
          <option value="">{{ $t('views.RiskAssessmentView.all_actions') }}</option>
          <option value="block">{{ $t('views.RiskAssessmentView.action_block') }}</option>
          <option value="warn">{{ $t('views.RiskAssessmentView.action_warn') }}</option>
          <option value="continue">{{ $t('views.RiskAssessmentView.action_continue') }}</option>
          <option value="escalate">{{ $t('views.RiskAssessmentView.action_escalate') }}</option>
        </select>
        <p v-if="facetDegraded" class="mt-1 text-xs text-warning" data-testid="risk-assessment-facet-degraded-note">
          {{ $t('views.RiskAssessmentView.facet_degraded_note') }}
        </p>
      </div>
    </div>

    <div class="flex items-center gap-2 pt-2 border-t" data-testid="risk-assessment-facet-actions">
      <Button
        variant="outline"
        @click="applyFilters"
        :disabled="isLoading"
        data-testid="risk-assessment-apply"
      >
        {{ $t('views.RiskAssessmentView.apply') }}
      </Button>
      <Button
        variant="ghost"
        @click="resetFilters"
        :disabled="isLoading"
        data-testid="risk-assessment-reset"
      >
        {{ $t('views.RiskAssessmentView.reset') }}
      </Button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, watch } from 'vue'
import Button from 'primevue/button'

interface FilterState {
  keyPrefix: string
  pipelineId: string
  days: number
  action: string
  limit: number
}

interface Props {
  filters: FilterState
  facetDegraded: boolean
  isLoading: boolean
  pipelines: Array<{ id: string; name: string }>
}

const props = defineProps<Props>()

const emit = defineEmits<{
  'update:filters': [filters: FilterState]
  apply: [filters: FilterState]
  reset: []
}>()

// Local reactive copy of filters to avoid mutating props directly
const localFilters = reactive<FilterState>({
  keyPrefix: '',
  pipelineId: '',
  days: 7,
  action: '',
  limit: 100,
})

// Sync local filters with props when props change
watch(
  () => props.filters,
  (newFilters) => {
    localFilters.keyPrefix = newFilters.keyPrefix
    localFilters.pipelineId = newFilters.pipelineId
    localFilters.days = newFilters.days
    localFilters.action = newFilters.action
    localFilters.limit = newFilters.limit
  },
  { immediate: true, deep: true },
)

// Emit updates when local filters change
watch(
  localFilters,
  (newFilters) => {
    emit('update:filters', { ...newFilters })
  },
  { deep: true },
)

function applyFilters(): void {
  emit('apply', { ...localFilters })
}

function resetFilters(): void {
  localFilters.keyPrefix = ''
  localFilters.pipelineId = ''
  localFilters.days = 7
  localFilters.action = ''
  localFilters.limit = 100
  emit('reset')
}
</script>
