<template>
  <div class="fixed right-4 top-16 z-40 w-96 max-w-[calc(100vw-2rem)] rounded-lg border bg-card p-4 shadow-lg">
    <div class="mb-3 flex items-center justify-between">
      <h3 class="text-sm font-semibold">{{ $t('components.PipelineSnapshotTimeline.version_timeline') }}</h3>
      <button
        type="button"
        class="rounded p-1 hover:bg-accent"
        :aria-label="$t('components.PipelineSnapshotTimeline.close')"
        data-testid="snapshot-timeline-close"
        @click="$emit('close')"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
    </div>

    <LoadingSpinner v-if="loading" />

    <template v-else>
      <p v-if="snapshots.length === 0" class="text-sm text-muted-foreground">{{ $t('components.PipelineSnapshotTimeline.no_snapshots_yet') }}</p>

      <div v-else class="mb-3 max-h-56 space-y-1 overflow-y-auto">
        <div
          v-for="s in snapshots"
          :key="s.id"
          role="button"
          tabindex="0"
          :data-testid="`snapshot-timeline-row-${s.id}`"
          class="flex items-center justify-between gap-2 rounded-md border px-2 py-1.5"
          @click="selectA(s.id)"
          @keydown.enter="selectA(s.id)"
          @keydown.space.prevent="selectA(s.id)"
        >
          <div class="flex min-w-0 items-center gap-2">
            <span class="text-xs font-medium">v{{ s.snapshot_version }}</span>
            <span
              class="rounded px-1.5 py-0.5 text-[10px] font-medium"
              :class="s.version_kind === 'edit' ? 'bg-primary/10 text-primary' : 'bg-muted/40 text-muted-foreground'"
            >
              {{ s.version_kind === 'edit' ? $t('components.PipelineSnapshotTimeline.edit') : $t('components.PipelineSnapshotTimeline.run') }}
            </span>
            <span
              v-if="s.channel && s.channel !== 'none'"
              class="rounded bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning"
            >
              {{ s.channel }}
            </span>
            <span class="truncate text-[10px] text-muted-foreground">{{ formatDate(s.created_at) }}</span>
          </div>
          <span class="shrink-0 text-[10px] text-muted-foreground">{{ s.tag || '' }}</span>
        </div>
      </div>

      <span class="mb-1 block text-[10px] text-muted-foreground">{{ $t('components.PipelineSnapshotTimeline.compare_to') }}</span>
      <div class="mb-3 flex items-center gap-2">
        <Select
          data-testid="snapshot-timeline-compare"
          aria-label="Base snapshot to compare"
          :placeholder="$t('components.PipelineSnapshotTimeline.select_base_snapshot')"
          :options="compareOptions"
          v-model="compareB"
        />
        <Button
          size="small"
          class="text-xs"
          :disabled="!canDiff"
          @click="runDiff"
          data-testid="snapshot-timeline-diff"
        >
          {{ $t('components.PipelineSnapshotTimeline.diff') }}
        </Button>
      </div>

      <div v-if="diffResult" class="space-y-2" data-testid="snapshot-timeline-diff-result">
        <div class="text-xs">
          <span class="font-medium">{{ $t('components.PipelineSnapshotTimeline.impacted_nodes') }}</span>
          <span class="text-muted-foreground">{{ impactedLabel ? impactedLabel : $t('components.PipelineSnapshotTimeline.none') }}</span>
        </div>

        <div v-if="diffResult.semantic?.breaking_changes?.length" class="space-y-1">
          <div class="text-xs font-medium text-destructive">{{ $t('components.PipelineSnapshotTimeline.breaking_changes') }}</div>
          <div
            v-for="(b, i) in diffResult.semantic.breaking_changes"
            :key="i"
            class="rounded-md border border-destructive/30 bg-destructive/5 px-2 py-1 text-[10px]"
          >
            <span :class="b.severity === 'block' ? 'font-medium text-destructive' : 'text-warning'">
              {{ b.severity }}
            </span>
            — {{ b.reason }}
          </div>
        </div>
        <p v-else class="text-[11px] text-muted-foreground">{{ $t('components.PipelineSnapshotTimeline.no_breaking_port_changes') }}</p>
      </div>

      <div class="mt-3 flex justify-end border-t pt-2">
        <Button
          size="small"
          severity="danger"
          outlined
          class="text-xs"
          :disabled="!selectedSnapshotId"
          @click="rollback"
          data-testid="snapshot-timeline-rollback"
        >
          {{ $t('components.PipelineSnapshotTimeline.rollback_to_selected') }}
        </Button>
      </div>
      <p v-if="actionError" class="mt-1 text-[11px] text-destructive">{{ actionError }}</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import Select from 'primevue/select'
import Button from 'primevue/button'
import LoadingSpinner from '../shared/LoadingSpinner.vue'
import { formatDateShort } from '../../lib/formatDate'
import { api } from '../../lib/api/client'

interface TimelineSnapshot {
  id: string
  snapshot_version: number
  tag: string | null
  created_at: string | null
  version_kind: string
  channel: string
  draft: boolean
}

const props = defineProps<{ pipelineId: string }>()
defineEmits<{ close: [] }>()

const loading = ref(true)
const snapshots = ref<TimelineSnapshot[]>([])
const compareB = ref<string | null>(null)
const diffResult = ref<Record<string, any> | null>(null)
const selectedSnapshotId = ref<string | null>(null)
const actionError = ref<string | null>(null)

const compareOptions = computed(() =>
  snapshots.value.map(s => ({ value: s.id, label: `v${s.snapshot_version} — ${s.version_kind}${s.tag ? ` — ${s.tag}` : ''}` })),
)

const canDiff = computed(() => compareB.value != null && selectedSnapshotId.value != null && compareB.value !== selectedSnapshotId.value)

const impactedLabel = computed(() => {
  const ids = diffResult.value?.semantic?.impacted_nodes ?? []
  if (!Array.isArray(ids) || ids.length === 0) return ''
  return ids.map((id: unknown) => String(id).slice(0, 8)).join(', ')
})

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '—'
  try {
    return formatDateShort(new Date(dateStr))
  } catch (e: unknown) {
    return dateStr
  }
}

function selectA(id: string): void {
  selectedSnapshotId.value = id
}

async function loadSnapshots(): Promise<void> {
  loading.value = true
  try {
    const { data } = await api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
      params: { path: { pipeline_id: props.pipelineId } },
    })
    snapshots.value = ((data as unknown as { items?: TimelineSnapshot[] })?.items ?? []) as TimelineSnapshot[]
  } catch (e: unknown) {
    snapshots.value = []
  } finally {
    loading.value = false
  }
}

async function runDiff(): Promise<void> {
  if (!selectedSnapshotId.value || !compareB.value) return
  actionError.value = null
  const { data, error } = await api.POST('/api/v1/pipelines/{pipeline_id}/snapshots/diff', {
    params: { path: { pipeline_id: props.pipelineId } },
    body: { snapshot_a_id: selectedSnapshotId.value, snapshot_b_id: compareB.value },
  })
  if (error) {
    actionError.value = String(error)
    return
  }
  diffResult.value = data as Record<string, any>
}

async function rollback(): Promise<void> {
  if (!selectedSnapshotId.value) return
  actionError.value = null
  const { error } = await api.POST('/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}/rollback', {
    params: { path: { pipeline_id: props.pipelineId, snapshot_id: selectedSnapshotId.value } },
  })
  if (error) {
    actionError.value = String(error)
    return
  }
  actionError.value = null
  await loadSnapshots()
}

onMounted(loadSnapshots)
</script>
