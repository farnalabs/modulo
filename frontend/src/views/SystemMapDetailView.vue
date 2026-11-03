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
          :revisions="revisions"
          :active-revision-id="activeRevision?.id"
          :active-map="activeMap"
          @select-revision="selectRevision"
          @new-draft="createNewDraft"
          @discover="runDiscovery"
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
const revisions = computed(() => store.revisions)
const discovery = computed(() => store.discovery)
const loadError = computed(() => store.error)
const loading = computed(() => store.loading)

const showSidebar = ref(true)
const discoveryOpen = ref(false)
const saving = ref(false)
const actionError = ref<string | null>(null)
const editedNodes = ref<SystemMapNode[]>([])
const editedRelationships = ref<SystemMapRelationship[]>([])

const nodeTypes = NODE_TYPES
const relationshipTypes = RELATIONSHIP_TYPES

const isPublished = computed(() => activeRevision.value?.status === 'published')
const isArchived = computed(() => activeRevision.value?.status === 'archived')
const readOnly = computed(() => isPublished.value || isArchived.value)

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

async function loadData(): Promise<void> {
  actionError.value = null
  await store.fetchAccess()
  await store.loadMap(mapId.value)
}

async function selectRevision(revisionId: string): Promise<void> {
  if (!activeMap.value) return
  try {
    await store.loadRevision(activeMap.value.id, revisionId)
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('systemMaps.errors.load_revision')
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
  if (!activeMap.value) return
  saving.value = true
  actionError.value = null
  try {
    await store.saveDraft(
      activeMap.value.id,
      nodePayload(editedNodes.value),
      relationshipPayload(editedRelationships.value),
      'Manual save',
    )
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
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('systemMaps.errors.publish_revision')
  }
}

async function archiveRevision(): Promise<void> {
  if (!activeMap.value || !activeRevision.value) return
  actionError.value = null
  try {
    await store.archive(activeMap.value.id, activeRevision.value.id)
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('systemMaps.errors.archive_revision')
  }
}

onMounted(loadData)
watch(() => route.params.id, loadData)
</script>
