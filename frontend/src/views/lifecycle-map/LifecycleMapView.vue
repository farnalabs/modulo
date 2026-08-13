<template>
  <div class="min-h-screen bg-background">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="mx-auto flex items-center justify-between gap-3 max-w-6xl">
        <div class="flex items-center gap-3">
          <router-link
            to="/lifecycle-maps"
            class="text-sm text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
            Back
          </router-link>
          <PageHeader :title="mapData?.name || 'Lifecycle Map'" />
          <span
            v-if="mapData"
            class="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
          >
            v{{ mapData.current_version }}
          </span>
        </div>
        <div class="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            :disabled="exporting || !mapData"
            :data-testid="'lifecycle-map-export'"
            @click="handleExport"
          >
            {{ exporting ? $t('views.LifecycleMapView.exporting') : $t('views.LifecycleMapView.export_map') }}
          </Button>
          <Button
            variant="outline"
            size="sm"
            :data-testid="'lifecycle-map-import'"
            @click="openImportDialog"
          >
            {{ $t('views.LifecycleMapView.import_map') }}
          </Button>
          <template v-if="mapData?.versions && mapData.versions.length > 1">
            <div class="flex items-center gap-2">
              <label for="lifecyclemapview-field-1" class="text-sm text-muted-foreground">{{ $t('views.LifecycleMapView.version_label') }}</label>
              <Select :aria-label="$t('views.LifecycleMapView.version_label')" v-model="selectedVersion" @update:model-value="onVersionChange">
                <SelectTrigger data-testid="lifecycle-map-version-select" class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm">
                  <SelectValue placeholder="Select version" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem v-for="v in sortedVersions" :key="v.version" :value="v.version">
                    v{{ v.version }}
                    <template v-if="v.created_by"> — {{ v.created_by }}</template>
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          </template>
          <Button
            variant="outline"
            size="sm"
            data-testid="lifecycle-map-view-edit"
            :aria-label="$t('views.LifecycleMapView.edit')"
            @click="editMap"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="mr-1"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg>
            {{ $t('views.LifecycleMapView.edit') }}
          </Button>
        </div>
        <p
          v-if="exportError"
          role="alert"
          class="mt-2 text-sm text-destructive"
          data-testid="lifecycle-map-export-error"
        >
          {{ exportError }}
        </p>
      </div>
    </header>

    <main class="page-wide">
      <div v-if="isLoadingDetail" class="flex items-center justify-center py-20">
        <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>

      <ErrorAlert
        v-else-if="detailError"
        :message="detailError"
        :on-retry="() => loadMap()"
        class="mb-6"
      />

      <template v-else-if="mapData">
        <div class="mb-4 flex items-start justify-between gap-4">
          <div class="flex-1">
            <p v-if="mapData.description" class="text-sm text-muted-foreground">
              {{ mapData.description }}
            </p>
            <div class="flex items-center gap-3 mt-2 text-xs text-muted-foreground">
              <span v-if="mapData.owner" class="flex items-center gap-1">
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
                {{ mapData.owner }}
              </span>
              <span>{{ (mapData.stages ?? []).length }} stages</span>
              <span>{{ graduatedCount }} graduated</span>
              <span>{{ manualCount }} manual</span>
            </div>
          </div>
        </div>

        <div class="rounded-xl border border-border bg-card overflow-hidden" style="height: 600px">
          <LifecycleMapRenderer
            :map-data="mapData"
            :journeys="store.journeys"
            :on-modulo-stage-click="handleModuloStageClick"
            :on-external-stage-click="handleExternalStageClick"
            @journey-open="openJourneyDetail"
          />
        </div>

        <section
          v-if="unattributedJourneys.length"
          class="mt-4 rounded-xl border border-dashed border-border bg-card p-4"
          aria-label="Unattributed journeys"
        >
          <h2 class="text-sm font-semibold text-foreground">
            {{ $t('views.LifecycleMapView.journey.unattributed_hint', { count: unattributedJourneys.length }) }}
          </h2>
          <p class="mt-1 text-xs text-muted-foreground">
            {{ $t('views.LifecycleMapView.journey.unattributed_desc') }}
          </p>
          <div class="mt-3 flex flex-wrap gap-2">
            <div
              v-for="journey in unattributedJourneys"
              :key="`${journey.kind}:${journey.ref}`"
              class="w-[240px]"
            >
              <JourneyCard :journey="journey" @open="openJourneyDetail(journey)" />
            </div>
          </div>
        </section>

        <ErrorAlert
          v-if="journeysError"
          :message="journeysError"
          :on-retry="loadJourneys"
          class="mt-4"
        />

        <section
          v-if="selectedJourneyKey"
          class="mt-4 rounded-xl border border-border bg-card p-4"
          aria-label="Journey details"
        >
          <div class="flex items-center justify-between gap-2">
            <h2 class="text-sm font-semibold text-foreground">
              {{ $t('views.LifecycleMapView.journey.detail_title', { journey: selectedJourneyLabel }) }}
            </h2>
            <Button
              variant="outline"
              size="sm"
              :aria-label="$t('views.LifecycleMapView.journey.close')"
              @click="closeJourneyDetail"
            >
              {{ $t('views.LifecycleMapView.journey.close') }}
            </Button>
          </div>

          <ErrorAlert
            v-if="journeyDetailError"
            :message="journeyDetailError"
            :on-retry="retryJourneyDetail"
            class="mt-3"
          />

          <p
            v-else-if="journeyDetail && journeyDetail.runs.length === 0"
            class="mt-3 text-sm text-muted-foreground"
          >
            {{ $t('views.LifecycleMapView.journey.no_runs') }}
          </p>

          <ul v-else-if="journeyDetail" class="mt-3 divide-y divide-border">
            <li
              v-for="run in journeyDetail.runs"
              :key="run.run_id"
              class="flex flex-wrap items-center justify-between gap-2 py-2 text-sm"
            >
              <span :class="statusBadgeClass(run.status ?? '')" class="badge capitalize">
                {{ statusLabel(run.status ?? '') }}
              </span>
              <ProvenanceBadge :provenance="run.provenance" />
              <span class="text-muted-foreground">{{ formatRunDate(run.completed_at) }}</span>
            </li>
          </ul>

          <div
            v-else
            class="mt-3 flex items-center gap-2 text-sm text-muted-foreground"
            role="status"
          >
            <div class="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
            {{ $t('views.LifecycleMapView.journey.loading') }}
          </div>
        </section>
      </template>

      <div v-else class="text-center py-20 text-muted-foreground">
        Lifecycle map not found.
      </div>
    </main>

    <!-- Import dialog -->
    <div
      role="presentation"
      v-if="showImportDialog"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="showImportDialog = false"
    >
      <div role="dialog" aria-modal="true" aria-labelledby="lifecyclemapview-import-title" class="w-full max-w-lg rounded-lg border bg-card p-6 shadow-lg">
        <h3 id="lifecyclemapview-import-title" class="mb-1 text-base font-semibold">
          {{ $t('views.LifecycleMapView.import_dialog_title') }}
        </h3>
        <p class="mb-3 text-sm text-muted-foreground">
          {{ $t('views.LifecycleMapView.import_paste_hint') }}
        </p>
        <textarea
          v-model="importPayload"
          rows="10"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs"
          :placeholder="$t('views.LifecycleMapView.import_placeholder')"
          data-testid="lifecycle-map-import-payload"
          :aria-label="$t('views.LifecycleMapView.import_payload_label')"
        />
        <div
          v-if="importError"
          role="alert"
          class="mt-3 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
        >
          {{ importError }}
        </div>
        <div class="mt-4 flex justify-end gap-2">
          <Button variant="outline" size="sm" @click="showImportDialog = false">
            {{ $t('views.LifecycleMapView.cancel') }}
          </Button>
          <Button
            size="sm"
            :disabled="!importPayload.trim() || importing"
            data-testid="lifecycle-map-import-confirm"
            @click="handleImportConfirm"
          >
            {{ importing ? $t('views.LifecycleMapView.importing') : $t('views.LifecycleMapView.import_map') }}
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import PageHeader from '../../components/shared/PageHeader.vue'
import { useLifecycleMapsStore } from '../../stores/lifecycleMaps'
import LifecycleMapRenderer from '../../components/lifecycle-map/LifecycleMapRenderer.vue'
import ProvenanceBadge from '../../components/lifecycle-map/ProvenanceBadge.vue'
import JourneyCard from '../../components/lifecycle-map/JourneyCard.vue'
import ErrorAlert from '../../components/shared/ErrorAlert.vue'
import { formatRunDate } from '../../utils/runUtils'
import { Button } from '@/components/ui/button'
import type { JourneySummary } from '../../types/lifecycleMap'
import type { LifecycleMapStage } from '../../stores/lifecycleMaps'
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '@/components/ui/select'
import { formatApiError } from '../../lib/api/formatError'

const route = useRoute()
const router = useRouter()
const store = useLifecycleMapsStore()
const { t } = useI18n()

const mapId = computed(() => route.params.id as string)
const selectedVersion = ref<number | null>(null)

const mapData = computed(() => store.currentMap)
const isLoadingDetail = computed(() => store.isLoadingDetail)
const detailError = computed(() => store.detailError)
const graduatedCount = computed(() => store.graduatedCount)
const manualCount = computed(() => store.manualCount)
const journeysError = computed(() => store.journeysError)
const unattributedJourneys = computed(() => store.unattributedJourneys)
const selectedJourneyKey = computed(() => store.selectedJourneyKey)
const journeyDetail = computed(() => store.journeyDetail)
const journeyDetailError = computed(() => store.journeyDetailError)

const selectedJourneyLabel = computed(() => {
  const key = selectedJourneyKey.value
  if (!key) return ''
  return key.replace(':', ' ')
})

const sortedVersions = computed(() => {
  const versions = mapData.value?.versions ?? []
  return [...versions].sort((a, b) => b.version - a.version)
})

async function loadMap(): Promise<void> {
  if (!mapId.value) return
  await store.fetchMap(mapId.value)
  if (store.currentMap) {
    selectedVersion.value = store.currentMap.current_version
  }
}

async function onVersionChange(): Promise<void> {
  if (!mapId.value || selectedVersion.value == null) return
  if (selectedVersion.value === mapData.value?.current_version) {
    await store.fetchMap(mapId.value)
  } else {
    await store.fetchMapVersion(mapId.value, selectedVersion.value)
  }
}

async function loadJourneys(): Promise<void> {
  if (!mapId.value) return
  await store.fetchJourneys(mapId.value)
}

async function openJourneyDetail(journey: JourneySummary): Promise<void> {
  if (!mapId.value) return
  await store.fetchJourneyDetail(mapId.value, journey.kind, journey.ref)
}

function closeJourneyDetail(): void {
  store.clearJourneyDetail()
}

async function retryJourneyDetail(): Promise<void> {
  if (!mapId.value || !selectedJourneyKey.value) return
  const idx = selectedJourneyKey.value.indexOf(':')
  if (idx === -1) return
  await store.fetchJourneyDetail(
    mapId.value,
    selectedJourneyKey.value.slice(0, idx),
    selectedJourneyKey.value.slice(idx + 1)
  )
}

function statusBadgeClass(status: string): string {
  switch (status) {
    case 'complete':
      return 'badge-context-green'
    case 'failed':
    case 'stalled':
    case 'eval_failed':
      return 'badge-context-rose'
    case 'running':
      return 'badge-context-blue'
    case 'awaiting_human':
    case 'claimed':
      return 'badge-context-amber'
    default:
      return 'badge-context-slate'
  }
}

function statusLabel(status: string): string {
  const key = `views.LifecycleMapView.journey.status.${status}`
  const translated = t(key)
  return translated === key ? status : translated
}

function editMap(): void {
  if (mapId.value) router.push({ name: 'lifecycle-map-editor', params: { id: mapId.value } })
}

function handleModuloStageClick(stage: LifecycleMapStage): void {
  if (stage.pipeline_id) {
    router.push({ name: 'pipeline-editor', params: { id: stage.pipeline_id } })
  }
}

function handleExternalStageClick(stage: LifecycleMapStage): void {
  if (stage.external_url) {
    window.open(stage.external_url, '_blank', 'noopener,noreferrer')
  }
}

const exporting = ref(false)
const exportError = ref<string | null>(null)

async function handleExport(): Promise<void> {
  if (!mapId.value || exporting.value) return
  exporting.value = true
  exportError.value = null
  try {
    const envelope = await store.exportMap(mapId.value)
    if (!envelope) return
    const json = JSON.stringify(envelope, null, 2)
    const blob = new Blob([json], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${envelope.name || 'lifecycle-map'}.lifecycle-map.json`
    link.click()
    URL.revokeObjectURL(url)
    try {
      await navigator.clipboard.writeText(json)
    } catch {
      // Clipboard may be unavailable (e.g. http); the download still succeeded.
    }
  } catch (e: unknown) {
    exportError.value = formatApiError(e)
  } finally {
    exporting.value = false
  }
}

const showImportDialog = ref(false)
const importPayload = ref('')
const importing = ref(false)
const importError = ref<string | null>(null)

function openImportDialog(): void {
  importPayload.value = ''
  importError.value = null
  showImportDialog.value = true
}

async function handleImportConfirm(): Promise<void> {
  if (!importPayload.value.trim() || importing.value) return
  importing.value = true
  importError.value = null
  try {
    const envelope = JSON.parse(importPayload.value)
    const created = await store.importMap(envelope)
    showImportDialog.value = false
    router.push({ name: 'lifecycle-map-detail', params: { id: created.id } })
  } catch (e: unknown) {
    if (e instanceof SyntaxError) {
      importError.value = t('views.LifecycleMapView.import_invalid_json')
    } else {
      importError.value = formatApiError(e)
    }
  } finally {
    importing.value = false
  }
}

onMounted(() => {
  if (mapId.value) {
    store.fetchMap(mapId.value).then(() => {
      if (store.currentMap) {
        selectedVersion.value = store.currentMap.current_version
      }
      loadJourneys()
    })
  }
})
</script>
