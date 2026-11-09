<template>
  <section class="card p-4 space-y-4" data-testid="determination-scan">
    <header class="flex flex-wrap items-baseline justify-between gap-2">
      <h2 class="text-lg font-semibold">{{ $t('views.DeterminationView.scan_title') }}</h2>
      <span v-if="scan" class="text-sm text-muted-foreground" data-testid="determination-scan-summary">
        {{ scan.summary }}
      </span>
    </header>

    <div
      v-if="loading"
      class="flex items-center gap-3 text-sm text-muted-foreground"
      aria-live="polite"
      data-testid="determination-scan-loading"
    >
      <LoadingSpinner />
      <span>{{ $t('views.DeterminationView.scan_loading') }}</span>
    </div>

    <ErrorAlert v-else-if="error" :message="error" :on-retry="onRetry" />

    <p v-else-if="!scan" class="text-sm text-muted-foreground" data-testid="determination-scan-empty">
      {{ $t('views.DeterminationView.scan_empty') }}
    </p>

    <template v-else>
      <section class="space-y-2" data-testid="determination-samples">
        <h3 class="text-sm font-medium">
          {{ $t('views.DeterminationView.samples_title') }}
          <span class="text-muted-foreground">
            ({{ $t('views.DeterminationView.samples_count', { count: scan.samples.length }) }})
          </span>
        </h3>
        <p v-if="scan.samples.length === 0" class="text-sm text-muted-foreground" data-testid="determination-samples-empty">
          {{ $t('views.DeterminationView.samples_empty') }}
        </p>
        <div v-else class="overflow-x-auto rounded-lg border">
          <table class="w-full text-left text-sm">
            <thead class="bg-muted/50">
              <tr>
                <th class="px-3 py-2 font-medium">{{ $t('views.DeterminationView.sample_type') }}</th>
                <th class="px-3 py-2 font-medium">{{ $t('views.DeterminationView.sample_resource') }}</th>
                <th class="px-3 py-2 font-medium">{{ $t('views.DeterminationView.sample_count') }}</th>
                <th class="px-3 py-2 font-medium">{{ $t('views.DeterminationView.sample_connector') }}</th>
                <th class="px-3 py-2 font-medium">{{ $t('views.DeterminationView.sample_error') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr
                v-for="sample in scan.samples"
                :key="`${sample.connector_id}-${sample.resource}`"
                data-testid="determination-sample-row"
              >
                <td class="px-3 py-2">{{ sample.connector_type }}</td>
                <td class="px-3 py-2">{{ sample.resource }}</td>
                <td class="px-3 py-2 tabular-nums">{{ sample.sample_count }}</td>
                <td class="px-3 py-2 font-mono text-xs break-all">{{ sample.connector_id }}</td>
                <td class="px-3 py-2">
                  <span v-if="sample.error" class="text-destructive-text">{{ sample.error }}</span>
                  <span v-else class="text-muted-foreground">{{ $t('views.DeterminationView.sample_no_error') }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="space-y-2" data-testid="determination-findings">
        <h3 class="text-sm font-medium">
          {{ $t('views.DeterminationView.findings_title') }}
          <span class="text-muted-foreground">
            ({{ $t('views.DeterminationView.findings_count', { count: scan.findings.length }) }})
          </span>
        </h3>
        <p v-if="scan.findings.length === 0" class="text-sm text-muted-foreground" data-testid="determination-findings-empty">
          {{ $t('views.DeterminationView.findings_empty') }}
        </p>
        <ul v-else class="space-y-2">
          <li
            v-for="(finding, index) in scan.findings"
            :key="`${finding.category}-${index}`"
            class="rounded-lg border bg-muted/20 p-3 space-y-1"
            data-testid="determination-finding-row"
          >
            <div class="flex flex-wrap items-center gap-2">
              <span class="rounded-full bg-muted px-2 py-0.5 text-xs font-medium">{{ finding.category }}</span>
              <span class="rounded-full px-2 py-0.5 text-xs font-medium" :class="confidenceBadgeClass(finding.confidence)">
                {{ $t('views.DeterminationView.confidence_' + finding.confidence) }}
              </span>
            </div>
            <p class="text-sm font-medium">{{ finding.finding }}</p>
            <p class="text-sm text-muted-foreground">
              <span class="font-medium">{{ $t('views.DeterminationView.finding_evidence') }}:</span> {{ finding.evidence }}
            </p>
            <p v-if="finding.uncertainty" class="text-sm text-muted-foreground">
              <span class="font-medium">{{ $t('views.DeterminationView.finding_uncertainty') }}:</span> {{ finding.uncertainty }}
            </p>
          </li>
        </ul>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import ErrorAlert from '../shared/ErrorAlert.vue'
import LoadingSpinner from '../shared/LoadingSpinner.vue'
import { confidenceBadgeClass } from './confidencePresentation'
import type { DeterminationScanResponse } from '../../composables/useDetermination'

defineProps<{
  scan: DeterminationScanResponse | null
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{ (event: 'retry'): void }>()

function onRetry(): void {
  emit('retry')
}
</script>
