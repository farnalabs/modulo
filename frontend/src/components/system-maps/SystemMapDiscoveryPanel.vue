<template>
  <div class="fixed inset-0 z-50" :data-testid="dataTestId">
    <button type="button" class="absolute inset-0 bg-black/50" :aria-label="$t('common.close')" @click="$emit('close')" data-testid="system-map-discovery-backdrop" />
    <dialog open class="absolute right-0 top-0 h-full w-full max-w-2xl m-0 bg-card shadow-2xl overflow-hidden flex flex-col" aria-labelledby="discovery-panel-title">
      <header class="border-b px-4 py-3 flex items-center justify-between">
        <h2 id="discovery-panel-title" class="font-semibold">{{ $t('views.SystemMapDetailView.discovery_title') }}</h2>
        <button type="button" class="rounded p-1 text-muted-foreground hover:text-foreground" @click="$emit('close')" :aria-label="$t('common.close')" data-testid="system-map-discovery-close">×</button>
      </header>
      <div class="flex-1 overflow-y-auto p-4">
        <p class="text-sm text-muted-foreground mb-4">{{ $t('views.SystemMapDetailView.discovery_results_hint') }}</p>
        <div v-if="discovery" class="space-y-4">
           <section class="rounded-lg border bg-muted/30 p-3" data-testid="system-map-discovery-provenance">
             <h3 class="text-sm font-medium">{{ $t('views.SystemMapDetailView.discovery_provenance') }}</h3>
             <dl class="mt-2 grid gap-1 text-sm sm:grid-cols-2">
               <div><dt class="text-muted-foreground">{{ $t('views.SystemMapDetailView.discovery_mode') }}</dt><dd>{{ discovery.ai_assisted ? $t('views.SystemMapDetailView.mode_ai_assisted') : $t('views.SystemMapDetailView.mode_deterministic') }}</dd></div>
               <div v-if="discovery.ai_assisted"><dt class="text-muted-foreground">{{ $t('views.SystemMapDetailView.discovery_backend') }}</dt><dd>{{ discovery.provenance?.selected_backend_id ? backendName(discovery.provenance.selected_backend_id) : $t('views.SystemMapDetailView.provenance_unavailable') }}</dd></div>

               <div v-if="discovery.ai_assisted"><dt class="text-muted-foreground">{{ $t('views.SystemMapDetailView.eval_identity') }}</dt><dd>{{ props.evalIdentity ?? $t('views.SystemMapDetailView.provenance_unavailable') }}</dd></div>
               <div v-if="discovery.provenance?.prompt_version"><dt class="text-muted-foreground">{{ $t('views.SystemMapDetailView.prompt_version') }}</dt><dd>{{ discovery.provenance.prompt_version }}</dd></div>
              <div v-if="discovery.provenance?.rubric_version"><dt class="text-muted-foreground">{{ $t('views.SystemMapDetailView.rubric_version') }}</dt><dd>{{ discovery.provenance.rubric_version }}</dd></div>
              <div v-if="discovery.provenance?.evaluator_version"><dt class="text-muted-foreground">{{ $t('views.SystemMapDetailView.evaluator_version') }}</dt><dd>{{ discovery.provenance.evaluator_version }}</dd></div>
              <div v-if="discovery.provenance?.input_hash"><dt class="text-muted-foreground">{{ $t('views.SystemMapDetailView.input_hash') }}</dt><dd class="break-all">{{ discovery.provenance.input_hash }}</dd></div>
              <div v-if="discovery.provenance?.output_hash"><dt class="text-muted-foreground">{{ $t('views.SystemMapDetailView.output_hash') }}</dt><dd class="break-all">{{ discovery.provenance.output_hash }}</dd></div>
            </dl>
           <div v-if="discovery.eval_gate_verdict && discovery.eval_gate_verdict !== 'not_requested'" class="mt-3 text-sm" :class="discovery.eval_gate_passed === true ? 'text-success-text' : 'text-destructive'" aria-live="assertive">
             <p>{{ discovery.eval_gate_passed === true ? $t('views.SystemMapDetailView.eval_passed') : $t('views.SystemMapDetailView.eval_blocked', { verdict: discovery.eval_gate_verdict }) }}</p>
             <p v-if="discovery.eval_gate_passed !== true" class="mt-1">{{ $t('views.SystemMapDetailView.deterministic_fallback_note') }}</p>
           </div>
           <div v-if="discovery.ai_assisted && !discovery.eval_gate_passed" class="mt-3 rounded border border-destructive bg-destructive/10 p-3 text-sm text-destructive" aria-live="assertive" data-testid="system-map-discovery-eval-blocked">
             {{ $t('views.SystemMapDetailView.ai_suggestions_blocked') }}
           </div>
          </section>
          <section class="rounded-lg border bg-muted/30 p-3">
            <h3 class="text-sm font-medium">{{ $t('views.SystemMapDetailView.sources_inspected') }}</h3>
            <ul class="mt-2 text-sm text-muted-foreground space-y-1">
              <li v-for="source in discovery.sources_inspected" :key="source.identifier">{{ source.kind }}: {{ source.name }} ({{ source.identifier }})</li>
            </ul>
          </section>
          <section class="rounded-lg border bg-muted/30 p-3">
            <h3 class="text-sm font-medium">{{ $t('views.SystemMapDetailView.inferred_nodes') }} ({{ discovery.inferred_nodes?.length ?? 0 }})</h3>
            <ul class="mt-2 space-y-2 max-h-64 overflow-y-auto">
              <li
                v-for="(node, index) in discovery.inferred_nodes"
                :key="index"
                class="rounded border bg-background p-3 flex items-center justify-between"
                :class="decisionClass(`node:${index}`)"
              >
                <div>
                  <p class="font-medium">{{ node.node_type }}: {{ node.display_name }}</p>
                  <p class="text-xs text-muted-foreground">{{ $t('views.SystemMapDetailView.node_key_label', { key: node.node_key }) }}</p>
                </div>
                <div class="flex gap-1">
                  <button type="button" class="rounded border px-2 py-1 text-xs" :class="activeClass(`node:${index}`, 'accepted')" :aria-pressed="decisions[`node:${index}`] === 'accepted'" :disabled="!canSave" @click="onAccept(`node:${index}`)" :data-testid="`system-map-accept-node-${node.node_key}`">{{ $t('views.SystemMapDetailView.accept') }}</button>
                  <button type="button" class="rounded border px-2 py-1 text-xs text-destructive" :class="activeClass(`node:${index}`, 'rejected')" :aria-pressed="decisions[`node:${index}`] === 'rejected'" :disabled="!canSave" @click="onReject(`node:${index}`)" :data-testid="`system-map-reject-node-${node.node_key}`">{{ $t('views.SystemMapDetailView.reject') }}</button>
                </div>
              </li>
            </ul>
            <p v-if="!discovery.inferred_nodes?.length" class="mt-2 text-sm text-muted-foreground">{{ $t('views.SystemMapDetailView.no_inferred_nodes') }}</p>
          </section>
          <section class="rounded-lg border bg-muted/30 p-3">
            <h3 class="text-sm font-medium">{{ $t('views.SystemMapDetailView.inferred_relationships') }} ({{ discovery.inferred_edges?.length ?? 0 }})</h3>
            <ul class="mt-2 space-y-2 max-h-64 overflow-y-auto">
              <li
                v-for="(edge, index) in discovery.inferred_edges"
                :key="index"
                class="rounded border bg-background p-3 flex items-center justify-between"
                :class="decisionClass(`edge:${index}`)"
              >
                <div class="text-sm">{{ edge.source_node_key }} → {{ edge.target_node_key }} <span class="text-muted-foreground">({{ edge.relationship_type }})</span></div>
                <div class="flex gap-1">
                  <button type="button" class="rounded border px-2 py-1 text-xs" :class="activeClass(`edge:${index}`, 'accepted')" :aria-pressed="decisions[`edge:${index}`] === 'accepted'" :disabled="!canSave" @click="onAccept(`edge:${index}`)" :data-testid="`system-map-accept-edge-${index}`">{{ $t('views.SystemMapDetailView.accept') }}</button>
                  <button type="button" class="rounded border px-2 py-1 text-xs text-destructive" :class="activeClass(`edge:${index}`, 'rejected')" :aria-pressed="decisions[`edge:${index}`] === 'rejected'" :disabled="!canSave" @click="onReject(`edge:${index}`)" :data-testid="`system-map-reject-edge-${index}`">{{ $t('views.SystemMapDetailView.reject') }}</button>
                </div>
              </li>
            </ul>
            <p v-if="!discovery.inferred_edges?.length" class="mt-2 text-sm text-muted-foreground">{{ $t('views.SystemMapDetailView.no_inferred_edges') }}</p>
          </section>
          <section v-if="discovery.skipped?.length" class="rounded-lg border bg-muted/30 p-3">
            <h3 class="text-sm font-medium">{{ $t('views.SystemMapDetailView.skipped_sources') }}</h3>
            <ul class="mt-2 text-sm text-muted-foreground space-y-1">
              <li v-for="(skipped, index) in discovery.skipped" :key="index">{{ skipped.source }}: {{ skipped.reason }}</li>
            </ul>
          </section>
          <section v-if="discovery.warnings?.length" class="rounded-lg border bg-destructive/10 p-3">
            <h3 class="text-sm font-medium text-destructive">{{ $t('views.SystemMapDetailView.warnings') }}</h3>
            <ul class="mt-2 text-sm text-destructive-text space-y-1">
              <li v-for="(warning, index) in discovery.warnings" :key="index">• {{ warning }}</li>
            </ul>
          </section>
        </div>
      </div>
       <footer class="border-t px-4 py-3 flex justify-end gap-2">
         <button type="button" class="rounded border px-3 py-1.5 text-sm" @click="$emit('close')" data-testid="system-map-discovery-cancel">{{ $t('common.cancel') }}</button>
         <button type="button" class="rounded bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50" :disabled="!canSave" @click="onSave" :data-testid="`system-map-discovery-save`">{{ $t('views.SystemMapDetailView.apply_changes') }}</button>
       </footer>
    </dialog>
  </div>
</template>
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { DiscoveryReport, DiscoverySelection } from '../../stores/systemMaps'

const props = defineProps<{ discovery: DiscoveryReport | null; dataTestId?: string; backendNames?: Record<string, string>; evalIdentity?: string }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'save', selections: DiscoverySelection): void }>()
const { t } = useI18n()
const canSave = computed(() => props.discovery?.ai_assisted !== true || props.discovery.eval_gate_passed === true)
function backendName(id: string): string {
  return props.backendNames?.[id] ?? t('views.SystemMapDetailView.provenance_unavailable')
}

type Decision = 'accepted' | 'rejected'
const decisions = ref<Record<string, Decision>>({})

function toggle(key: string, decision: Decision): void {
  const next = { ...decisions.value }
  if (next[key] === decision) delete next[key]
  else next[key] = decision
  decisions.value = next
}
function onAccept(key: string): void { toggle(key, 'accepted') }
function onReject(key: string): void { toggle(key, 'rejected') }
function decisionClass(key: string): string {
  const decision = decisions.value[key]
  if (decision === 'accepted') return 'border-success bg-success/5'
  if (decision === 'rejected') return 'border-destructive bg-destructive/5 opacity-70'
  return ''
}
function activeClass(key: string, decision: Decision): string {
  if (decisions.value[key] !== decision) return ''
  return decision === 'accepted' ? 'border-success text-success-text' : 'border-destructive text-destructive'
}
function onSave(): void {
  const acceptedNodes: number[] = []
  const acceptedEdges: number[] = []
  const rejectedNodes: number[] = []
  const rejectedEdges: number[] = []
  for (const [key, decision] of Object.entries(decisions.value)) {
    const [kind, index] = key.split(':')
    const parsed = Number(index)
    if (Number.isNaN(parsed)) continue
    if (kind === 'node') (decision === 'accepted' ? acceptedNodes : rejectedNodes).push(parsed)
    else if (kind === 'edge') (decision === 'accepted' ? acceptedEdges : rejectedEdges).push(parsed)
  }
  emit('save', { acceptedNodes, acceptedEdges, rejectedNodes, rejectedEdges })
}
</script>
