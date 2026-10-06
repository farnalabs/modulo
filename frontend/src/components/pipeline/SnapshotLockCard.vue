<template>
  <!-- FAR-1287 Part 3: operator surface for a wedged pipeline snapshot advisory
       lock. The owning view mounts this behind `v-if="isSystemAdmin"`, so a
       non-system-admin never renders it and never issues the admin-only GET. -->
  <section
    class="border-b bg-card px-3 py-2"
    data-testid="pipeline-editor-snapshot-lock"
    :aria-label="$t('views.PipelineEditorView.snapshot_lock_title')"
  >
    <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
      <h3 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {{ $t('views.PipelineEditorView.snapshot_lock_title') }}
      </h3>
      <p class="min-w-0 flex-1 text-[11px] text-muted-foreground">
        {{ $t('views.PipelineEditorView.snapshot_lock_help') }}
      </p>
      <button
        type="button"
        :class="btnIcon"
        :disabled="loading"
        :aria-label="$t('views.PipelineEditorView.snapshot_lock_refresh')"
        :title="$t('views.PipelineEditorView.snapshot_lock_refresh')"
        data-testid="pipeline-editor-snapshot-lock-refresh"
        @click="load"
      >
        <RefreshCwIcon class="h-3.5 w-3.5" aria-hidden="true" />
      </button>
      <button
        v-if="isHeld"
        type="button"
        :class="btnDanger"
        :disabled="releasing"
        data-testid="pipeline-editor-snapshot-lock-release"
        @click="confirmOpen = true"
      >
        {{
          releasing
            ? $t('views.PipelineEditorView.snapshot_lock_releasing')
            : $t('views.PipelineEditorView.snapshot_lock_release')
        }}
      </button>
    </div>

    <!-- Async status + release result: one polite live region (A11Y). -->
    <div
      role="status"
      aria-live="polite"
      class="mt-1 flex flex-wrap items-center gap-2 text-xs"
      data-testid="pipeline-editor-snapshot-lock-status"
    >
      <span v-if="loading" class="text-muted-foreground">
        {{ $t('views.PipelineEditorView.snapshot_lock_checking') }}
      </span>
      <span v-else-if="isHeld" class="badge badge-status-destructive">{{ heldLabel }}</span>
      <span v-else-if="statusKnown" class="text-muted-foreground">
        {{ $t('views.PipelineEditorView.snapshot_lock_not_held') }}
      </span>
      <span v-if="releaseResultText" class="font-medium text-foreground">{{ releaseResultText }}</span>
    </div>

    <!-- Failures (load or release, including the typed 403): assertive region. -->
    <div
      v-if="errorMessage"
      role="alert"
      aria-live="assertive"
      class="mt-1 text-xs text-destructive"
      data-testid="pipeline-editor-snapshot-lock-error"
    >
      {{ errorMessage }}
    </div>

    <div
      v-if="holders.length > 0"
      class="mt-1 overflow-x-auto"
      data-testid="pipeline-editor-snapshot-lock-holders"
    >
      <table class="w-full text-xs">
        <caption class="sr-only">
          {{ $t('views.PipelineEditorView.snapshot_lock_holders_caption') }}
        </caption>
        <thead>
          <tr class="border-b text-left text-muted-foreground">
            <th scope="col" class="py-1 pr-3 font-medium">
              {{ $t('views.PipelineEditorView.snapshot_lock_pid') }}
            </th>
            <th scope="col" class="py-1 pr-3 font-medium">
              {{ $t('views.PipelineEditorView.snapshot_lock_application') }}
            </th>
            <th scope="col" class="py-1 pr-3 font-medium">
              {{ $t('views.PipelineEditorView.snapshot_lock_state') }}
            </th>
            <th scope="col" class="py-1 pr-3 font-medium">
              {{ $t('views.PipelineEditorView.snapshot_lock_since') }}
            </th>
            <th scope="col" class="py-1 font-medium">
              {{ $t('views.PipelineEditorView.snapshot_lock_query_start') }}
            </th>
          </tr>
        </thead>
        <tbody class="divide-y divide-border">
          <tr v-for="holder in holders" :key="holder.pid">
            <td class="py-1 pr-3 font-mono">{{ holder.pid }}</td>
            <td class="py-1 pr-3">
              {{ holder.application_name || $t('views.PipelineEditorView.snapshot_lock_unknown') }}
            </td>
            <td class="py-1 pr-3">
              {{ holder.state || $t('views.PipelineEditorView.snapshot_lock_unknown') }}
            </td>
            <td class="py-1 pr-3 whitespace-nowrap">
              {{ formatDateShortWithTime(holder.backend_start) }}
            </td>
            <td class="py-1 whitespace-nowrap">
              {{ formatDateShortWithTime(holder.query_start) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <FormDialog
      v-model:open="confirmOpen"
      :title="$t('views.PipelineEditorView.snapshot_lock_release_confirm_title')"
      :confirmText="$t('views.PipelineEditorView.snapshot_lock_release_confirm')"
      :loading="releasing"
      @confirm="releaseLock"
    >
      <p class="text-sm text-muted-foreground">
        {{ $t('views.PipelineEditorView.snapshot_lock_release_confirm_body') }}
      </p>
    </FormDialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { RefreshCw as RefreshCwIcon } from '@lucide/vue'
import FormDialog from '../shared/FormDialog.vue'
import { api } from '../../lib/api/client'
import type { components } from '../../lib/api/client'
import { formatApiError, isProblemDetail } from '../../lib/api/formatError'
import { formatDateShortWithTime } from '../../lib/formatDate'

type SnapshotLockStatus = components['schemas']['SnapshotLockStatusResponse']
type SnapshotLockHolder = components['schemas']['SnapshotLockHolder']
type SnapshotLockRelease = components['schemas']['SnapshotLockReleaseResponse']

const props = defineProps<{ pipelineId: string }>()

const { t } = useI18n()

const loading = ref(true)
const status = ref<SnapshotLockStatus | null>(null)
const errorMessage = ref<string | null>(null)
const releasing = ref(false)
const confirmOpen = ref(false)
const releaseResult = ref<SnapshotLockRelease | null>(null)

const holders = computed<SnapshotLockHolder[]>(() => status.value?.holders ?? [])
const isHeld = computed(() => status.value?.held === true)
const statusKnown = computed(() => status.value !== null)
const heldLabel = computed(() => {
  const count = holders.value.length
  return t('views.PipelineEditorView.snapshot_lock_held', { count })
})
const releaseResultText = computed(() => {
  const result = releaseResult.value
  if (!result) return ''
  if (result.released === 0) {
    return t('views.PipelineEditorView.snapshot_lock_released_none')
  }
  const head = t('views.PipelineEditorView.snapshot_lock_released', {
    count: result.released,
  })
  const pids = t('views.PipelineEditorView.snapshot_lock_released_pids', {
    pids: result.pids.join(', '),
  })
  return `${head} ${pids}`
})

const btnIcon =
  'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50'
const btnDanger =
  'inline-flex h-6 shrink-0 items-center justify-center whitespace-nowrap rounded-md border border-destructive/50 bg-destructive/10 px-2 text-[11px] font-medium text-destructive transition-colors hover:bg-destructive/20 disabled:cursor-not-allowed disabled:opacity-50'

/**
 * Read-only status probe. Failures land in the assertive region verbatim, so a
 * 403 from the GET surfaces its own detail (which names the required system
 * permission) instead of a generic string.
 */
async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = null
  try {
    const { data, error } = await api.GET('/api/v1/admin/pipelines/{pipeline_id}/snapshot-lock', {
      params: { path: { pipeline_id: props.pipelineId } },
    })
    if (error) {
      status.value = null
      errorMessage.value = t('views.PipelineEditorView.snapshot_lock_load_failed', {
        error: formatApiError(error),
      })
    } else {
      status.value = data ?? null
    }
  } catch (e: unknown) {
    status.value = null
    errorMessage.value = t('views.PipelineEditorView.snapshot_lock_load_failed', {
      error: formatApiError(e),
    })
  } finally {
    loading.value = false
  }
}

/**
 * Terminate the backend(s) holding the lock. The endpoint refuses with a typed
 * 403 (no organisation context, or the runtime database role lacks
 * `pg_signal_backend`); both arms are surfaced with the server's own detail so
 * the operator sees the exact grant to run.
 */
async function releaseLock(): Promise<void> {
  releasing.value = true
  errorMessage.value = null
  try {
    const { data, error, response } = await api.POST(
      '/api/v1/admin/pipelines/{pipeline_id}/snapshot-lock/release',
      { params: { path: { pipeline_id: props.pipelineId } } },
    )
    if (error) {
      // Both refusal arms answer HTTP 403; the problem-detail check also
      // catches a typed 403 body whose transport status did not survive.
      const body = error as unknown
      const forbidden =
        response?.status === 403 || (isProblemDetail(body) && body.status === 403)
      errorMessage.value = forbidden
        ? t('views.PipelineEditorView.snapshot_lock_release_forbidden', {
            error: formatApiError(error),
          })
        : t('views.PipelineEditorView.snapshot_lock_release_failed', {
            error: formatApiError(error),
          })
      confirmOpen.value = false
      return
    }
    releaseResult.value = data ?? { released: 0, pids: [] }
    confirmOpen.value = false
    await load()
  } catch (e: unknown) {
    errorMessage.value = t('views.PipelineEditorView.snapshot_lock_release_failed', {
      error: formatApiError(e),
    })
    confirmOpen.value = false
  } finally {
    releasing.value = false
  }
}

onMounted(load)
</script>
