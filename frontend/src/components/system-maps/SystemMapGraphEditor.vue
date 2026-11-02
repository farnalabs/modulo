<template>
  <div class="h-full overflow-auto bg-muted/20 p-6" :data-testid="dataTestId">
    <div class="mb-4 flex flex-wrap items-center gap-2 rounded-lg border bg-card p-3" aria-label="Graph controls">
      <span class="text-sm font-medium">{{ $t('views.SystemMapDetailView.node_types') }}</span>
      <button
        v-for="type in nodeTypes"
        :key="type"
        type="button"
        class="rounded border px-2 py-1 text-xs capitalize hover:bg-accent disabled:opacity-50"
        :disabled="readOnly"
        @click="addNode(type)"
        :data-testid="`system-map-add-node-${type}`"
      >{{ type.replace('_', ' ') }}</button>
      <span class="ml-2 text-sm font-medium">{{ $t('views.SystemMapDetailView.relationship_types') }}</span>
      <select v-model="relationshipType" class="rounded border px-2 py-1 text-xs" :disabled="readOnly" aria-label="Relationship type" data-testid="system-map-relationship-type">
        <option v-for="type in relationshipTypes" :key="type" :value="type">{{ type.replace('_', ' ') }}</option>
      </select>
      <button type="button" class="rounded border px-2 py-1 text-xs" :disabled="readOnly || localNodes.length < 2" @click="addRelationship" data-testid="system-map-add-relationship">{{ $t('views.SystemMapDetailView.add_relationship') }}</button>
    </div>

    <p class="mb-4 text-xs text-muted-foreground" aria-live="polite">{{ $t('views.SystemMapDetailView.bounded_editor_note') }}</p>

    <div class="relative min-h-[28rem] rounded-lg border bg-background p-6">
      <div v-if="localNodes.length === 0" class="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
        {{ $t('views.SystemMapDetailView.add_node_hint') }}
      </div>
      <div
        v-for="node in localNodes"
        :key="node.node_key"
        class="absolute w-48 rounded-lg border bg-card p-3 shadow-sm"
        :style="{ left: `${Number(node.position_x) + 24}px`, top: `${Number(node.position_y) + 24}px` }"
        :data-testid="`system-map-node-${node.node_key}`"
      >
        <div class="flex items-start justify-between gap-2">
          <div class="min-w-0">
            <span class="text-[10px] uppercase tracking-wide text-muted-foreground">{{ node.node_type }}</span>
            <input v-model="node.display_name" class="mt-1 w-full truncate bg-transparent text-sm font-medium outline-none" :aria-label="$t('views.SystemMapDetailView.node_name')" :disabled="readOnly" @change="emitNodes" />
          </div>
          <button type="button" class="rounded p-1 text-muted-foreground hover:text-destructive disabled:opacity-50" :disabled="readOnly" :aria-label="$t('views.SystemMapDetailView.remove_node', { name: node.display_name })" @click="removeNode(node.node_key)" :data-testid="`system-map-remove-node-${node.node_key}`">×</button>
        </div>
        <input v-model="node.config_json.label" class="mt-2 w-full rounded border bg-background px-2 py-1 text-xs" :placeholder="$t('views.SystemMapDetailView.metadata_placeholder')" :aria-label="$t('views.SystemMapDetailView.metadata')" :disabled="readOnly" @change="emitNodes" />
      </div>

      <svg v-if="localRelationships.length" class="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">
        <line v-for="relationship in localRelationships" :key="relationship.id" x1="10" y1="10" x2="220" y2="120" stroke="currentColor" stroke-opacity=".35" stroke-width="2" />
      </svg>
    </div>

    <div v-if="localRelationships.length" class="mt-4 rounded-lg border bg-card p-3" data-testid="system-map-relationships">
      <h2 class="text-sm font-medium">{{ $t('views.SystemMapDetailView.relationships') }}</h2>
      <ul class="mt-2 divide-y">
        <li v-for="relationship in localRelationships" :key="relationship.id" class="flex items-center justify-between py-2 text-xs">
          <span>{{ relationship.source_node_key }} → {{ relationship.target_node_key }} <span class="text-muted-foreground">({{ relationship.relationship_type }})</span></span>
          <button type="button" class="rounded px-2 py-1 text-destructive hover:bg-destructive/10 disabled:opacity-50" :disabled="readOnly" @click="removeRelationship(relationship.id)" :aria-label="$t('views.SystemMapDetailView.remove_relationship')" :data-testid="`system-map-remove-relationship-${relationship.id}`">{{ $t('views.SystemMapDetailView.remove') }}</button>
        </li>
      </ul>
    </div>
  </div>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { SystemMapNode, SystemMapRelationship, NodeType, RelationshipType } from '../../stores/systemMaps'
const props = defineProps<{ nodes: SystemMapNode[]; relationships: SystemMapRelationship[]; loopConfigs: unknown[]; nodeTypes: readonly NodeType[]; relationshipTypes: readonly RelationshipType[]; readOnly?: boolean; dataTestId?: string }>()
const emit = defineEmits<{ (e: 'nodes-change', nodes: Array<Record<string, unknown>>): void; (e: 'relationships-change', relationships: Array<Record<string, unknown>>): void }>()
const localNodes = ref<Array<SystemMapNode & { config_json: Record<string, unknown> }>>([])
const localRelationships = ref<SystemMapRelationship[]>([])
const relationshipType = ref<RelationshipType>(props.relationshipTypes[0])
const readOnly = computed(() => props.readOnly === true)
watch(() => props.nodes, (nodes) => { localNodes.value = nodes.map((node) => ({ ...node, config_json: { ...node.config_json } })) }, { immediate: true, deep: true })
watch(() => props.relationships, (relationships) => { localRelationships.value = relationships.map((relationship) => ({ ...relationship, config_json: { ...relationship.config_json } })) }, { immediate: true, deep: true })
function emitNodes(): void { emit('nodes-change', localNodes.value as unknown as Array<Record<string, unknown>>) }
function addNode(nodeType: NodeType): void { const index = localNodes.value.length + 1; localNodes.value.push({ id: `local-${Date.now()}`, organisation_id: '', revision_id: '', node_key: `${nodeType}-${index}`, node_type: nodeType, display_name: `${nodeType.replace('_', ' ')} ${index}`, config_json: {}, pipeline_id: null, connector_instance_id: null, owner_team_id: null, eval_id: null, position_x: String(40 + (index % 3) * 220), position_y: String(40 + Math.floor(index / 3) * 140), deleted_at: null, deleted_by: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString() }); emitNodes() }
function removeNode(nodeKey: string): void { localNodes.value = localNodes.value.filter((node) => node.node_key !== nodeKey); localRelationships.value = localRelationships.value.filter((relationship) => relationship.source_node_key !== nodeKey && relationship.target_node_key !== nodeKey); emitNodes(); emitRelationships() }
function addRelationship(): void { const [source, target] = localNodes.value; if (!source || !target) return; localRelationships.value.push({ id: `local-rel-${Date.now()}`, organisation_id: '', revision_id: '', source_node_key: source.node_key, target_node_key: target.node_key, relationship_type: relationshipType.value, config_json: {}, deleted_at: null, deleted_by: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString() }); emitRelationships() }
function removeRelationship(id: string): void { localRelationships.value = localRelationships.value.filter((relationship) => relationship.id !== id); emitRelationships() }
function emitRelationships(): void { emit('relationships-change', localRelationships.value as unknown as Array<Record<string, unknown>>) }
</script>
