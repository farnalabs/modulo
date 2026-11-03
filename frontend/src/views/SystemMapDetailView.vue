<template>
  <div class="h-[calc(100vh-3.5rem)] bg-background flex flex-col">
    <BackLink to="/system-maps" :label="$t('views.SystemMapsView.title')" data-testid="system-map-detail-back" />

    <div class="flex-1 flex min-h-0 overflow-hidden relative">
      <!-- Sidebar: Revision History -->
      <div
        v-if="showSidebar"
        class="absolute inset-y-0 left-0 z-30 w-80 max-w-full border-r bg-card shadow-lg flex flex-col lg:static lg:z-auto lg:shadow-none"
        :data-testid="`system-map-revision-sidebar`"
        :aria-label="$t('views.SystemMapDetailView.revision_history')"
      >
        <RevisionSidebar
          :map-id="mapId"
          :revisions="historyPageRevisions"
          :active-revision-id="activeRevision?.id"
          :active-map="activeMap"
          :current-revision-id="currentRevisionId"
          :loading="historyPending"
          :error="historyError"
          :page="historyPage"
          :page-count="historyPageCount"
          :total="historyTotal"
          :by-time-loading="byTimeLoading"
          :by-time-requested-at="byTimeRequestedAt"
          :by-time-unavailable="byTimeUnavailable"
          :by-time-error="byTimeError"
          :historical-at="historicalAt"
          @select-revision="selectRevision"
          @select-current="selectCurrentRevision"
          @select-by-time="selectRevisionByTime"
          @new-draft="createNewDraft"
          @discover="runDiscovery"
          @page-change="changeHistoryPage"
          @retry-history="reloadHistory"
          @compare="openDiffPanel"
        />
      </div>

      <!-- Main Editor Area -->
      <main class="flex-1 flex flex-col min-w-0 overflow-hidden relative">
        <ErrorAlert
          v-if="loadError"
          :message="loadError"
          :on-retry="loadData"
          class="m-3"
        />
        <ErrorAlert
          v-else-if="actionError"
          :message="actionError"
          :on-dismiss="clearActionError"
          :dismiss-label="$t('common.dismiss')"
          class="m-3"
        />

        <HeaderBar
          :map="activeMap"
          :revision="activeRevision"
          :saving="saving"
          :read-only="readOnly"
          @toggle-sidebar="showSidebar = !showSidebar"
          @save="saveDraft"
          @publish="publishRevision"
          @archive="archiveRevision"
          @discover="runDiscovery"
          data-testid="system-map-detail-header"
        />

        <div class="flex-1 flex overflow-hidden relative">
          <!-- Graph Editor -->
          <div class="flex-1 relative" :data-testid="`system-map-graph-editor`">
            <!-- ux-conformance STATE-1/STATE-3: the live region announces ONLY
                 the loading status text. <output> carries the implicit `status`
                 role, so an explicit role="status" is deliberately omitted
                 (FAR-1812 / SonarCloud Web:S6819); `aria-busy` sits on the
                 spinner placeholder it describes, never on the live region. -->
            <div
              v-if="loading"
              class="flex flex-col items-center justify-center h-full gap-3"
              data-testid="system-map-detail-loading"
            >
              <output class="text-sm text-muted-foreground" aria-live="polite">{{ $t('views.SystemMapDetailView.loading') }}</output>
              <LoadingSpinner aria-busy="true" />
            </div>

            <!-- The editor renders for ANY loaded graph, including one with zero
                 nodes: it owns the node-type toolbar, so hiding it for an empty
                 draft would leave no way to add the first node. -->
            <GraphEditor
              v-else-if="graph"
              :nodes="graph.nodes"
              :relationships="graph.relationships"
              :loop-configs="graph.loop_configs"
              :node-types="nodeTypes"
              :relationship-types="relationshipTypes"
              :read-only="readOnly"
              @nodes-change="onNodesChange"
              @relationships-change="onRelationshipsChange"
              data-testid="system-map-graph-canvas"
            />

            <div v-else-if="!loadError" class="flex items-center justify-center h-full text-muted-foreground" data-testid="system-map-empty-state">
              <div class="text-center">
                <p class="text-lg">{{ $t('views.SystemMapDetailView.no_graph') }}</p>
                <p class="text-sm mt-2">{{ $t('views.SystemMapDetailView.no_graph_hint') }}</p>
              </div>
            </div>
          </div>

          <!-- Discovery Review Panel (slide-over) -->
          <DiscoveryPanel
            v-if="discoveryOpen"
            :discovery="discovery"
            @close="discoveryOpen = false"
            @save="saveDraftWithDiscovery"
            data-testid="system-map-discovery-panel"
          />

          <!-- Revision comparison slide-over (diff + restore-as-new) -->
          <RevisionDiffPanel
            v-if="diffOpen"
            :map-id="mapId"
            :target-revision="activeRevision"
            :revisions="store.revisionHistory.map((entry) => entry.revision)"
            :base-revision-id="diffBaseRevisionId"
            :diff="revisionDiff"
            :loading="revisionDiffLoading"
            :error="revisionDiffError"
            :restore-loading="restoreLoading"
            :restore-error="restoreError"
            :restore-success="restoreSuccess"
            @close="closeDiffPanel"
            @compare="compareAgainst"
            @restore="restoreRevision"
            @retry="runDiff"
            @dismiss-restore-error="clearRestoreError"
          />
        </div>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useSystemMapsStore, NODE_TYPES, RELATIONSHIP_TYPES } from '../stores/systemMaps'
import type { DiscoverySelection, SystemMapNode, SystemMapRelationship } from '../stores/systemMaps'
import BackLink from '../components/BackLink.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import HeaderBar from '../components/system-maps/SystemMapHeaderBar.vue'
import GraphEditor from '../components/system-maps/SystemMapGraphEditor.vue'
import RevisionSidebar from '../components/system-maps/SystemMapRevisionSidebar.vue'
import RevisionDiffPanel from '../components/system-maps/SystemMapRevisionDiffPanel.vue'
import DiscoveryPanel from '../components/system-maps/SystemMapDiscoveryPanel.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import { mergeDiscoverySelections } from '../components/system-maps/discoveryMerge'

const route = useRoute()
const store = useSystemMapsStore()
const { t } = useI18n()

const mapId = computed(() => route.params.id as string)
const activeMap = computed(() => store.activeMap)
const activeRevision = computed(() => store.activeRevision)
const graph = computed(() => store.graph)
const discovery = computed(() => store.discovery)
const loadError = computed(() => store.error)
const loading = computed(() => store.loading)

// Revision history (the API's full revision set, paged client-side).
const historyPageRevisions = computed(() => store.revisionHistoryPageRevisions)
const historyLoading = computed(() => store.revisionHistoryLoading)
// Never read emptiness as "loaded": until the history request settles, show the
// skeleton rather than a false "no revisions" empty state. A failed map load
// stops the history request from ever starting, so that case falls through to
// the empty state (the main area owns the error and its retry).
const historyPending = computed(() =>
  historyLoading.value
  || (!store.revisionHistoryLoaded && !store.revisionHistoryError && !store.error),
)
const historyError = computed(() => store.revisionHistoryError)
const historyPage = computed(() => store.revisionHistoryPage)
const historyPageCount = computed(() => store.revisionHistoryPageCount)
const historyTotal = computed(() => store.revisionHistoryTotal)
const currentRevisionId = computed(() => store.currentRevisionId)
const byTimeLoading = computed(() => store.byTimeLoading)
const byTimeRequestedAt = computed(() => store.byTimeRequestedAt)
const byTimeUnavailable = computed(() => store.byTimeUnavailable)
const byTimeError = computed(() => store.byTimeError)

// Diff + restore.
const diffOpen = ref(false)
const diffBaseRevisionId = ref<string | null>(null)
const revisionDiff = computed(() => store.revisionDiff)
const revisionDiffLoading = computed(() => store.revisionDiffLoading)
const revisionDiffError = computed(() => store.revisionDiffError)
const restoreLoading = computed(() => store.restoreLoading)
const restoreError = computed(() => store.restoreError)
const restoreSuccess = ref<string | null>(null)

const showSidebar = ref(true)
const discoveryOpen = ref(false)
const saving = ref(false)
const actionError = ref<string | null>(null)
const editedNodes = ref<SystemMapNode[]>([])
const editedRelationships = ref<SystemMapRelationship[]>([])
/**
 * Browsing the history (or a point-in-time revision) is a read-only preview:
 * it must never be editable in place. A new draft is how an operator starts
 * from an existing revision.
 */
const previewReadOnly = ref(false)

const nodeTypes = NODE_TYPES
const relationshipTypes = RELATIONSHIP_TYPES

const isPublished = computed(() => activeRevision.value?.status === 'published')
const isArchived = computed(() => activeRevision.value?.status === 'archived')
const readOnly = computed(() => previewReadOnly.value || isPublished.value || isArchived.value)

/** Set only while the displayed revision came from the point-in-time lookup. */
const historicalAt = computed(() => {
  const byTime = store.byTimeRevision
  if (!byTime || activeRevision.value?.id !== byTime.id) return null
  return store.byTimeRequestedAt
})

watch(graph, (value) => {
  editedNodes.value = (value?.nodes ?? []).map((node) => ({ ...node, config_json: { ...node.config_json } }))
  editedRelationships.value = (value?.relationships ?? []).map((relationship) => ({ ...relationship, config_json: { ...relationship.config_json } }))
}, { immediate: true, deep: true })

function clearActionError(): void { actionError.value = null }

function nodePayload(nodes: SystemMapNode[]) {
  return nodes.map((node) => ({
    node_key: node.node_key,
    node_type: node.node_type,
    display_name: node.display_name,
    config_json: node.config_json,
    pipeline_id: node.pipeline_id,
    connector_instance_id: node.connector_instance_id,
    owner_team_id: node.owner_team_id,
    eval_id: node.eval_id,
    position_x: node.position_x,
    position_y: node.position_y,
  }))
}

function relationshipPayload(relationships: SystemMapRelationship[]) {
  return relationships.map((relationship) => ({
    source_node_key: relationship.source_node_key,
    target_node_key: relationship.target_node_key,
    relationship_type: relationship.relationship_type,
    config_json: relationship.config_json,
  }))
}

async function reloadHistory(): Promise<void> {
  await store.fetchRevisionHistory(mapId.value)
}

async function loadData(): Promise<void> {
  actionError.value = null
  previewReadOnly.value = false
  store.clearByTimeRevision()
  await store.fetchAccess()
  await store.loadMap(mapId.value)
  if (store.error) return
  await reloadHistory()
}

async function selectRevision(revisionId: string): Promise<void> {
  if (!activeMap.value) return
  store.clearByTimeRevision()
  try {
    await store.loadRevision(activeMap.value.id, revisionId)
    // Only a revision that actually loaded becomes the read-only preview.
    previewReadOnly.value = true
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('systemMaps.errors.load_revision')
  }
}

async function selectCurrentRevision(): Promise<void> {
  if (!activeMap.value) return
  actionError.value = null
  previewReadOnly.value = false
  store.clearByTimeRevision()
  try {
    await store.loadCurrentRevision(activeMap.value.id)
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('systemMaps.errors.load_current_revision')
  }
}

function changeHistoryPage(nextPage: number): void {
  store.setRevisionHistoryPage(nextPage)
}

/** Resolve and display the revision that was active at `at` (an absolute instant). */
async function selectRevisionByTime(at: string): Promise<void> {
  if (!activeMap.value) return
  actionError.value = null
  try {
    await store.loadRevisionByTime(activeMap.value.id, at)
    previewReadOnly.value = true
  } catch (err) {
    // A by-time 404 is the honest empty state, not a failure: the store records
    // it as `byTimeUnavailable`, so no error alert is raised for it.
    if (store.byTimeUnavailable) return
    actionError.value = err instanceof Error ? err.message : t('systemMaps.errors.load_revision_at_time')
  }
}

async function createNewDraft(): Promise<void> {
  if (!activeMap.value || !activeRevision.value) return
  saving.value = true
  actionError.value = null
  try {
    await store.saveDraft(
      activeMap.value.id,
      nodePayload(editedNodes.value),
      relationshipPayload(editedRelationships.value),
      `Draft from revision ${activeRevision.value.revision_number}`,
    )
    previewReadOnly.value = false
    store.clearByTimeRevision()
    await reloadHistory()
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('systemMaps.errors.create_draft')
  } finally {
    saving.value = false
  }
}

async function runDiscovery(): Promise<void> {
  if (!activeMap.value) return
  actionError.value = null
  try {
    await store.discover(`Discovered ${activeMap.value.name}`, `Discovery at ${new Date().toISOString()}`)
    discoveryOpen.value = true
    await reloadHistory()
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('systemMaps.errors.discovery_failed')
  }
}

function onNodesChange(nodes: Array<Record<string, unknown>>): void {
  editedNodes.value = nodes as unknown as SystemMapNode[]
}

function onRelationshipsChange(relationships: Array<Record<string, unknown>>): void {
  editedRelationships.value = relationships as unknown as SystemMapRelationship[]
}

async function saveDraft(): Promise<void> {
  if (!activeMap.value || previewReadOnly.value) return
  saving.value = true
  actionError.value = null
  try {
    await store.saveDraft(
      activeMap.value.id,
      nodePayload(editedNodes.value),
      relationshipPayload(editedRelationships.value),
      'Manual save',
    )
    await reloadHistory()
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('systemMaps.errors.save_draft')
  } finally {
    saving.value = false
  }
}

async function saveDraftWithDiscovery(selections: DiscoverySelection): Promise<void> {
  const merged = mergeDiscoverySelections(editedNodes.value, editedRelationships.value, discovery.value, selections)
  editedNodes.value = merged.nodes
  editedRelationships.value = merged.relationships

  await saveDraft()
  if (!actionError.value) discoveryOpen.value = false
}

async function publishRevision(): Promise<void> {
  if (!activeMap.value || !activeRevision.value) return
  actionError.value = null
  try {
    await store.publish(activeMap.value.id, activeRevision.value.id)
    previewReadOnly.value = false
    await reloadHistory()
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('systemMaps.errors.publish_revision')
  }
}

async function archiveRevision(): Promise<void> {
  if (!activeMap.value || !activeRevision.value) return
  actionError.value = null
  try {
    await store.archive(activeMap.value.id, activeRevision.value.id)
    previewReadOnly.value = false
    await reloadHistory()
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('systemMaps.errors.archive_revision')
  }
}

/** Default comparison base: the published revision, else the newest other one. */
function defaultBaseRevisionId(targetId: string): string | null {
  const others = store.revisionHistory
    .map((entry) => entry.revision)
    .filter((revision) => revision.id !== targetId)
    .sort((a, b) => b.revision_number - a.revision_number)
  const published = others.find((revision) => revision.id === currentRevisionId.value)
  return published?.id ?? others[0]?.id ?? null
}

async function runDiff(): Promise<void> {
  if (!activeMap.value || !activeRevision.value || !diffBaseRevisionId.value) return
  await store.diffRevisions(activeMap.value.id, diffBaseRevisionId.value, activeRevision.value.id)
}

async function openDiffPanel(): Promise<void> {
  if (!activeRevision.value) return
  diffOpen.value = true
  restoreSuccess.value = null
  store.clearRestoreState()
  await store.fetchCurrentRevision(mapId.value)
  diffBaseRevisionId.value = defaultBaseRevisionId(activeRevision.value.id)
  if (diffBaseRevisionId.value) await runDiff()
}

function closeDiffPanel(): void {
  diffOpen.value = false
}

async function compareAgainst(baseRevisionId: string): Promise<void> {
  diffBaseRevisionId.value = baseRevisionId
  await runDiff()
}

function clearRestoreError(): void {
  store.clearRestoreError()
}

/**
 * Restore a published/archived revision as a NEW DRAFT and navigate to it.
 * It never publishes: the operator must review and publish explicitly.
 */
async function restoreRevision(revisionId: string): Promise<void> {
  if (!activeMap.value) return
  restoreSuccess.value = null
  const sourceNumber = store.revisionHistory
    .map((entry) => entry.revision)
    .find((revision) => revision.id === revisionId)?.revision_number
  try {
    const created = await store.restoreRevisionAsNew(activeMap.value.id, revisionId)
    previewReadOnly.value = false
    store.clearByTimeRevision()
    await store.loadRevision(activeMap.value.id, created.id)
    restoreSuccess.value = t('views.SystemMapDetailView.restore_success', {
      number: created.revision_number,
      source: sourceNumber ?? '—',
    })
  } catch {
    // The store retains the localized message in restoreError, which the panel
    // renders inline; nothing here should erase it.
  }
}

async function loadAndOpenDiscovery(): Promise<void> {
  await loadData()
  // The dashboard deep-links here after running discovery so the inferred
  // draft is reviewed straight away; the report only opens when it belongs to
  // this navigation (the query flag), not on any later visit to the map.
  if (route.query.discovery === '1' && store.discovery) discoveryOpen.value = true
}

onMounted(loadAndOpenDiscovery)
watch(() => route.params.id, loadData)
</script>
