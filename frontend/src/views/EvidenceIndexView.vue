<template>
  <div class="page-narrow space-y-6" data-testid="evidence-index-page">
    <PageHeader :title="$t('views.EvidenceIndexView.title')" :subtitle="$t('views.EvidenceIndexView.subtitle')">
      <template #actions>
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-muted disabled:opacity-50"
          data-testid="evidence-index-refresh"
          :disabled="store.isLoading"
          @click="store.fetchIndex"
        >
          {{ store.isLoading ? $t('views.EvidenceIndexView.refreshing') : $t('views.EvidenceIndexView.refresh') }}
        </button>
      </template>
    </PageHeader>

    <div v-if="store.isLoading" aria-live="polite" data-testid="evidence-index-loading" class="flex items-center gap-3 text-sm text-muted-foreground">
      <LoadingSpinner />
      <span>{{ $t('views.EvidenceIndexView.loading') }}</span>
    </div>
    <ErrorAlert v-else-if="store.error" :message="store.error" :on-retry="store.fetchIndex" />
    <template v-else-if="store.index">
      <section class="grid gap-3 sm:grid-cols-3" :aria-label="$t('views.EvidenceIndexView.summary_label')">
        <div class="rounded-lg border bg-card p-4 shadow-sm"><div class="text-xs text-muted-foreground">{{ $t('views.EvidenceIndexView.pack_version') }}</div><div class="mt-1 font-medium">{{ store.index.pack_version }}</div></div>
        <div class="rounded-lg border bg-card p-4 shadow-sm"><div class="text-xs text-muted-foreground">{{ $t('views.EvidenceIndexView.generated_at') }}</div><div class="mt-1 font-medium">{{ formatDate(store.index.generated_at) }}</div></div>
        <div class="rounded-lg border bg-card p-4 shadow-sm"><div class="text-xs text-muted-foreground">{{ $t('views.EvidenceIndexView.coverage') }}</div><div class="mt-1 font-medium">{{ store.index.complete ? $t('views.EvidenceIndexView.complete') : $t('views.EvidenceIndexView.incomplete') }}</div></div>
      </section>

      <section class="space-y-3" aria-labelledby="evidence-artifacts-heading">
        <div class="flex items-center justify-between"><h2 id="evidence-artifacts-heading" class="text-lg font-semibold">{{ $t('views.EvidenceIndexView.artifacts_title') }}</h2><span aria-live="polite" class="text-sm text-muted-foreground">{{ store.index.artifacts.length }}</span></div>
        <EmptyState v-if="!store.index.artifacts.length" :title="$t('views.EvidenceIndexView.empty_title')" :description="$t('views.EvidenceIndexView.empty_description')" />
        <div v-else class="overflow-x-auto rounded-lg border bg-card shadow-sm">
          <table class="w-full text-left text-sm"><thead><tr class="border-b bg-muted/50"><th scope="col" class="px-3 py-2 font-medium">{{ $t('views.EvidenceIndexView.col_type') }}</th><th scope="col" class="px-3 py-2 font-medium">{{ $t('views.EvidenceIndexView.col_status') }}</th><th scope="col" class="px-3 py-2 font-medium">{{ $t('views.EvidenceIndexView.col_provenance') }}</th><th scope="col" class="px-3 py-2 font-medium">{{ $t('views.EvidenceIndexView.col_records') }}</th><th scope="col" class="px-3 py-2 font-medium">{{ $t('views.EvidenceIndexView.col_location') }}</th></tr></thead>
            <tbody><tr v-for="artifact in store.index.artifacts" :key="artifact.artifact_type" class="border-b last:border-b-0 align-top"><td class="px-3 py-2 font-medium">{{ artifact.artifact_type }}</td><td class="px-3 py-2"><span class="rounded-full bg-muted px-2 py-1 text-xs">{{ artifact.status }}</span><div v-if="artifact.absence_reason" class="mt-1 text-xs text-muted-foreground">{{ artifact.absence_reason }}</div></td><td class="px-3 py-2">{{ artifact.provenance }}</td><td class="px-3 py-2">{{ artifact.record_count ?? '—' }}</td><td class="max-w-sm break-words px-3 py-2 font-mono text-xs">{{ artifact.location }}</td></tr></tbody>
          </table>
        </div>
      </section>
      <p v-if="store.index.missing_artifact_types.length" aria-live="polite" class="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">{{ $t('views.EvidenceIndexView.missing', { count: store.index.missing_artifact_types.length }) }}</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { useEvidenceStore } from '../stores/evidence'

const store = useEvidenceStore()
const formatDate = (value: string): string => new Date(value).toLocaleString()
onMounted(() => { void store.fetchIndex() })
</script>
