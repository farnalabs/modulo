<template>
  <div class="h-[calc(100vh-3.5rem)] bg-background flex flex-col">
    <BackLink to="/system-maps" :label="$t('views.SystemMapsView.title')" data-testid="system-map-detail-back" />

    <div class="flex-1 flex min-h-0 overflow-hidden">
      <!-- Sidebar: Revision History -->
      <aside
        v-if="showSidebar"
        class="w-80 border-r bg-card flex flex-col hidden lg:flex"
        :data-testid="`system-map-revision-sidebar`"
        aria-label="Revision history"
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
      </aside>

      <!-- Main Editor Area -->
      <main class="flex-1 flex flex-col min-w-0 overflow-hidden relative">
        <HeaderBar
          :map="activeMap"
          :revision="activeRevision"
          :graph="graph"
          :loading="loading"
          :saving="saving"
          :show-sidebar="showSidebar"
          @toggle-sidebar="showSidebar = !showSidebar"
          @save="saveDraft"
          @publish="publishRevision"
          @archive="archiveRevision"
          @discover="runDiscovery"
          @rescan="runRescan"
          data-testid="system-map-detail-header"
        />

        <div class="flex-1 flex overflow-hidden relative">
          <!-- Graph Editor -->
          <div class="flex-1 relative" :data-testid="`system-map-graph-editor`">
            <GraphEditor
              v-if="graph"
              :nodes="graph.nodes"
              :relationships="graph.relationships"
              :loop-configs="graph.loop_configs"
              :node-types="nodeTypes"
              :relationship-types="relationshipTypes"
              :read-only="isPublished || isArchived"
              @nodes-change="onNodesChange"
              @relationships-change="onRelationshipsChange"
              data-testid="system-map-graph-canvas"
            />

            <div v-else class="flex items-center justify-center h-full text-muted-foreground" data-testid="system-map-empty-state">
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
            :graph="graph"
            @close="discoveryOpen = false"
            @accept="acceptSuggestion"
            @reject="rejectSuggestion"
            @save="saveDraftWithDiscovery"
            data-testid="system-map-discovery-panel"
          />
        </div>

        <!-- Rescan Diff Dialog -->
        <RescanDialog
          v-if="rescanDialog.open"
          :diff="rescanDialog.diff"
          :loading="rescanDialog.loading"
          @close="rescanDialog = { open: false, diff: null, loading: false }"
          @apply="applyRescan"
          data-testid="system-map-rescan-dialog"
        />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useSystemMapsStore, NODE_TYPES, RELATIONSHIP_TYPES } from '../stores/systemMaps'
import BackLink from '../components/BackLink.vue'
import HeaderBar from '../components/system-maps/SystemMapHeaderBar.vue'
import GraphEditor from '../components/system-maps/SystemMapGraphEditor.vue'
import RevisionSidebar from '../components/system-maps/SystemMapRevisionSidebar.vue'
import DiscoveryPanel from '../components/system-maps/SystemMapDiscoveryPanel.vue'
import RescanDialog from '../components/system-maps/SystemMapRescanDialog.vue'
const route = useRoute()
const store = useSystemMapsStore()

const mapId = computed(() => route.params.id as string)
const activeMap = computed(() => store.activeMap)
const activeRevision = computed(() => store.activeRevision)
const graph = computed(() => store.graph)
const revisions = computed(() => store.revisions)
const discovery = computed(() => store.discovery)
const loading = computed(() => store.loading)

const showSidebar = ref(true)
const discoveryOpen = ref(false)
const saving = ref(false)
const rescanDialog = ref<{ open: boolean; diff: { added_nodes?: unknown[]; removed_nodes?: unknown[]; added_relationships?: unknown[]; removed_relationships?: unknown[]; ambiguous_matches?: unknown[] } | null; loading: boolean }>({ open: false, diff: null, loading: false })

const nodeTypes = NODE_TYPES
const relationshipTypes = RELATIONSHIP_TYPES

const isPublished = computed(() => activeRevision.value?.status === 'published')
const isArchived = computed(() => activeRevision.value?.status === 'archived')

async function loadData(): Promise<void> {
  await store.fetchAccess()
  await store.loadMap(mapId.value)
}

async function selectRevision(revisionId: string): Promise<void> {
  if (activeMap.value) await store.loadRevision(activeMap.value.id, revisionId)
}

async function createNewDraft(): Promise<void> {
  if (!activeMap.value || !activeRevision.value) return
  // Create a new draft revision based on current revision
  const nodes = (graph.value?.nodes ?? []).map((n) => ({
    node_key: n.node_key,
    node_type: n.node_type,
    display_name: n.display_name,
    config_json: n.config_json,
    pipeline_id: n.pipeline_id,
    connector_instance_id: n.connector_instance_id,
    owner_team_id: n.owner_team_id,
    eval_id: n.eval_id,
    position_x: n.position_x,
    position_y: n.position_y,
  }))
  const relationships = (graph.value?.relationships ?? []).map((r) => ({
    source_node_key: r.source_node_key,
    target_node_key: r.target_node_key,
    relationship_type: r.relationship_type,
    config_json: r.config_json,
  }))
  await store.saveDraft(activeMap.value.id, nodes, relationships, `Draft from revision ${activeRevision.value.revision_number}`)
}

async function runDiscovery(): Promise<void> {
  if (!activeMap.value) return
  try {
    await store.discover(`Discovered ${activeMap.value.name}`, `Discovery at ${new Date().toISOString()}`)
    discoveryOpen.value = true
  } catch (err) {
    console.error('Discovery failed:', err)
  }
}

async function saveDraft(): Promise<void> {
  if (!activeMap.value || !graph.value) return
  saving.value = true
  try {
    const nodes = graph.value.nodes.map((n) => ({
      node_key: n.node_key,
      node_type: n.node_type,
      display_name: n.display_name,
      config_json: n.config_json,
      pipeline_id: n.pipeline_id,
      connector_instance_id: n.connector_instance_id,
      owner_team_id: n.owner_team_id,
      eval_id: n.eval_id,
      position_x: n.position_x,
      position_y: n.position_y,
    }))
    const relationships = graph.value.relationships.map((r) => ({
      source_node_key: r.source_node_key,
      target_node_key: r.target_node_key,
      relationship_type: r.relationship_type,
      config_json: r.config_json,
    }))
    await store.saveDraft(activeMap.value.id, nodes, relationships, 'Manual save')
  } catch (err) {
    console.error('Save failed:', err)
  } finally { saving.value = false }
}

async function publishRevision(): Promise<void> {
  if (!activeMap.value || !activeRevision.value) return
  try { await store.publish(activeMap.value.id, activeRevision.value.id) } catch (err) { console.error('Publish failed:', err) }
}

async function archiveRevision(): Promise<void> {
  if (!activeMap.value || !activeRevision.value) return
  try { await store.archive(activeMap.value.id, activeRevision.value.id) } catch (err) { console.error('Archive failed:', err) }
}

function onNodesChange(_nodes: Array<Record<string, unknown>>): void { /* GraphEditor handles local state */ }
function onRelationshipsChange(_relationships: Array<Record<string, unknown>>): void { /* GraphEditor handles local state */ }

async function acceptSuggestion(_nodeKey: string): Promise<void> { /* DiscoveryPanel owns review state. */ }
async function rejectSuggestion(_nodeKey: string): Promise<void> { /* DiscoveryPanel owns review state. */ }

async function saveDraftWithDiscovery(): Promise<void> {
  await saveDraft()
  discoveryOpen.value = false
}

async function runRescan(): Promise<void> {
  if (!activeMap.value) return
  rescanDialog.value = { open: true, diff: null, loading: true }
  // TODO: call rescan endpoint
  // For now, mock the diff
  setTimeout(() => {
    rescanDialog.value.diff = {
      added_nodes: [],
      removed_nodes: [],
      added_relationships: [],
      removed_relationships: [],
      ambiguous_matches: [],
    }
    rescanDialog.value.loading = false
  }, 1000)
}

async function applyRescan(): Promise<void> {
  // Apply rescan changes to current draft
  rescanDialog.value = { open: false, diff: null, loading: false }
  await saveDraft()
}

onMounted(loadData)
watch(() => route.params.id, loadData)
</script>
