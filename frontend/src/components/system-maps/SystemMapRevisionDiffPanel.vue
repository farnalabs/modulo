<template>
  <div class="fixed inset-0 z-50" :data-testid="dataTestId">
    <button
      type="button"
      class="absolute inset-0 bg-black/50"
      :aria-label="$t('common.close')"
      data-testid="system-map-revision-diff-backdrop"
      @click="$emit('close')"
    />
    <aside
      class="absolute right-0 top-0 flex h-full w-full max-w-2xl flex-col overflow-hidden bg-card shadow-2xl"
      role="dialog"
      aria-modal="true"
      aria-labelledby="revision-diff-title"
      data-testid="system-map-revision-diff-panel"
    >
      <header class="flex items-center justify-between border-b px-4 py-3">
        <h2 id="revision-diff-title" class="font-semibold">{{ $t('views.SystemMapDetailView.diff_title') }}</h2>
        <button
          type="button"
          class="rounded p-1 text-muted-foreground hover:text-foreground"
          :aria-label="$t('common.close')"
          data-testid="system-map-revision-diff-close"
          @click="$emit('close')"
        >×</button>
      </header>

      <div class="space-y-3 border-b px-4 py-3">
        <div class="space-y-1">
          <label :for="baseSelectId" class="block text-xs font-medium">{{ $t('views.SystemMapDetailView.diff_base_label') }}</label>
          <div data-testid="system-map-revision-diff-base">
            <AppSelect
              :id="baseSelectId"
              :model-value="baseRevisionId"
              :options="baseOptions"
              option-value="value"
              option-label="label"
              :placeholder="$t('views.SystemMapDetailView.diff_base_placeholder')"
              @update:model-value="onBaseChange"
            />
          </div>
        </div>
        <TableActions :actions="panelActions" />
        <p v-if="restoreSuccess" class="text-xs text-success-text" aria-live="polite" data-testid="system-map-revision-restore-success">
          {{ restoreSuccess }}
        </p>
        <div v-if="restoreError" data-testid="system-map-revision-restore-error">
          <ErrorAlert :message="restoreError" :on-dismiss="dismissRestoreError" :dismiss-label="$t('common.dismiss')" />
        </div>
      </div>

      <div class="flex-1 overflow-y-auto px-4 py-3">
        <div v-if="loading" data-testid="system-map-revision-diff-loading">
          <TableSkeleton :columns="3" :rows="4" :aria-label="$t('views.SystemMapDetailView.diff_loading')" />
        </div>

        <EmptyState
          v-else-if="!hasComparableRevisions"
          :title="$t('views.SystemMapDetailView.diff_empty_title')"
          :description="$t('views.SystemMapDetailView.diff_empty_body')"
          data-testid="system-map-revision-diff-empty"
        />

        <div v-else-if="error" data-testid="system-map-revision-diff-error">
          <ErrorAlert :message="error" :on-retry="retryDiff" />
        </div>

        <div v-else-if="diff" class="space-y-4" data-testid="system-map-revision-diff-summary">
          <p class="text-sm font-medium">
            {{ $t('views.SystemMapDetailView.diff_summary', { base: diff.base_revision_number, target: diff.target_revision_number }) }}
          </p>
          <p v-if="totalChanges === 0 && !diff.content_json_changed" class="text-sm text-muted-foreground" data-testid="system-map-revision-diff-no-changes">
            {{ $t('views.SystemMapDetailView.diff_no_changes') }}
          </p>
          <p v-if="diff.content_json_changed" class="text-xs text-muted-foreground" data-testid="system-map-revision-diff-content-changed">
            {{ $t('views.SystemMapDetailView.diff_content_changed') }}
          </p>

          <section
            v-for="axis in axes"
            :key="axis.key"
            class="rounded-lg border bg-muted/30 p-3"
            :data-testid="axis.testid"
          >
            <h3 class="text-sm font-medium">
              {{ axis.label }}
              <span class="ml-1 text-xs font-normal text-muted-foreground">({{ axis.total }})</span>
            </h3>
            <div class="mt-2 grid gap-2 sm:grid-cols-3">
              <div v-for="bucket in axis.buckets" :key="bucket.key" :data-testid="`${axis.testid}-${bucket.key}`">
                <p class="text-[10px] font-medium uppercase text-muted-foreground">
                  {{ bucket.label }} ({{ bucket.items.length }})
                </p>
                <ul v-if="bucket.items.length" class="mt-1 space-y-0.5 text-xs">
                  <li v-for="item in bucket.items" :key="item" class="break-all">{{ item }}</li>
                </ul>
                <p v-else class="mt-1 text-xs text-muted-foreground">{{ $t('views.SystemMapDetailView.diff_none') }}</p>
              </div>
            </div>
          </section>
        </div>
      </div>
    </aside>

    <FormDialog
      v-model:open="restoreOpen"
      :title="restoreDialogTitle"
      :description="restoreDialogDescription"
      :confirm-text="$t('views.SystemMapDetailView.restore_confirm')"
      :loading="restoreLoading"
      :confirm-disabled="!targetRevision"
      confirm-test-id="system-map-revision-restore-confirm"
      cancel-test-id="system-map-revision-restore-cancel"
      @confirm="confirmRestore"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { SystemMapRevision, SystemMapRevisionDiff } from '../../stores/systemMaps'
import AppSelect from '../shared/AppSelect.vue'
import EmptyState from '../shared/EmptyState.vue'
import ErrorAlert from '../shared/ErrorAlert.vue'
import FormDialog from '../shared/FormDialog.vue'
import TableActions from '../shared/TableActions.vue'
import TableSkeleton from '../shared/TableSkeleton.vue'

interface DiffBucket {
  key: 'added' | 'removed' | 'modified'
  label: string
  items: string[]
}

interface DiffAxis {
  key: string
  label: string
  testid: string
  total: number
  buckets: DiffBucket[]
}

const props = withDefaults(defineProps<{
  mapId: string
  targetRevision: SystemMapRevision | null
  revisions: SystemMapRevision[]
  baseRevisionId: string | null
  diff: SystemMapRevisionDiff | null
  loading?: boolean
  error?: string | null
  restoreLoading?: boolean
  restoreError?: string | null
  restoreSuccess?: string | null
  dataTestId?: string
}>(), {
  loading: false,
  error: null,
  restoreLoading: false,
  restoreError: null,
  restoreSuccess: null,
  dataTestId: 'system-map-revision-diff',
})

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'compare', baseRevisionId: string): void
  (e: 'restore', revisionId: string): void
  (e: 'retry'): void
  (e: 'dismiss-restore-error'): void
}>()

const { t } = useI18n()

const baseSelectId = `system-map-diff-base-${props.mapId}`
const restoreOpen = ref(false)

/**
 * Only revisions OTHER than the target can be the "before" side of a
 * comparison. When there is none, the panel says so instead of offering a
 * meaningless self-comparison.
 */
const baseOptions = computed(() =>
  props.revisions
    .filter((revision) => revision.id !== props.targetRevision?.id)
    .map((revision) => ({
      value: revision.id,
      label: t('views.SystemMapDetailView.diff_option', {
        number: revision.revision_number,
        status: t(`views.SystemMapDetailView.status_${revision.status === 'published' || revision.status === 'archived' ? revision.status : 'draft'}`),
      }),
    })),
)

const hasComparableRevisions = computed(() => baseOptions.value.length > 0)

const panelActions = computed(() => [{
  key: 'restore',
  label: t('views.SystemMapDetailView.restore_as_new'),
  onClick: () => { restoreOpen.value = true },
  disabled: !props.targetRevision || props.restoreLoading,
  testid: 'system-map-revision-restore',
}])

const restoreDialogTitle = computed(() =>
  t('views.SystemMapDetailView.restore_dialog_title', { number: props.targetRevision?.revision_number ?? '—' }),
)
const restoreDialogDescription = computed(() =>
  t('views.SystemMapDetailView.restore_dialog_description', { number: props.targetRevision?.revision_number ?? '—' }),
)

function readList(value: string[] | undefined): string[] {
  return Array.isArray(value) ? value : []
}

const axes = computed<DiffAxis[]>(() => {
  const diff = props.diff
  const build = (key: string, label: string, testid: string, added?: string[], removed?: string[], modified?: string[]): DiffAxis => {
    const buckets: DiffBucket[] = [
      { key: 'added', label: t('views.SystemMapDetailView.diff_added'), items: readList(added) },
      { key: 'removed', label: t('views.SystemMapDetailView.diff_removed'), items: readList(removed) },
      { key: 'modified', label: t('views.SystemMapDetailView.diff_modified'), items: readList(modified) },
    ]
    return {
      key,
      label,
      testid,
      total: buckets.reduce((sum, bucket) => sum + bucket.items.length, 0),
      buckets,
    }
  }
  return [
    build('nodes', t('views.SystemMapDetailView.diff_nodes'), 'system-map-revision-diff-nodes', diff?.nodes_added, diff?.nodes_removed, diff?.nodes_modified),
    build('relationships', t('views.SystemMapDetailView.diff_relationships'), 'system-map-revision-diff-relationships', diff?.relationships_added, diff?.relationships_removed, diff?.relationships_modified),
    build('loop_configs', t('views.SystemMapDetailView.diff_loop_configs'), 'system-map-revision-diff-loop-configs', diff?.loop_configs_added, diff?.loop_configs_removed, diff?.loop_configs_modified),
  ]
})

const totalChanges = computed(() => axes.value.reduce((sum, axis) => sum + axis.total, 0))

function onBaseChange(value: unknown): void {
  if (typeof value !== 'string' || value.length === 0) return
  emit('compare', value)
}

function retryDiff(): void {
  emit('retry')
}

function dismissRestoreError(): void {
  emit('dismiss-restore-error')
}

function confirmRestore(): void {
  if (!props.targetRevision) return
  restoreOpen.value = false
  emit('restore', props.targetRevision.id)
}
</script>
