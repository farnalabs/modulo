<template>
  <div class="flex h-[calc(100vh-3.5rem)]">
    <div v-if="loading" class="flex flex-1 items-center justify-center">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>
    <div v-else-if="pageError" class="flex flex-1 items-center justify-center">
      <div class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">{{ pageError }}</div>
    </div>
    <template v-else>
      <div class="relative flex-1">
        <!-- Empty-state overlay on top of the canvas -->
        <div v-if="flowNodes.length === 0" class="absolute inset-0 z-20 flex flex-col items-center justify-center gap-4 pointer-events-none">
          <div class="text-center">
            <h2 class="text-xl font-semibold">{{ pipeline?.name || 'Pipeline' }}</h2>
            <p v-if="pipeline?.description" class="mt-1 text-sm text-muted-foreground">{{ pipeline.description }}</p>
            <p class="mt-4 text-sm italic text-muted-foreground/60 select-none">no components in pipeline</p>
          </div>
          <div class="flex items-center gap-2 pointer-events-auto">
            <Button size="small" class="text-xs" @click="openRenameDialog">{{ $t('views.PipelineEditorView.rename') }}</Button>
            <button v-if="!pipeline?.archived_at" type="button" class="rounded-md border border-input bg-background px-3 py-1 text-xs font-medium hover:bg-accent" @click="handleArchive">{{ $t('views.PipelineEditorView.archive') }}</button>
            <button v-else type="button" class="rounded-md border border-input bg-background px-3 py-1 text-xs font-medium hover:bg-accent" @click="handleUnarchive">{{ $t('views.PipelineEditorView.unarchive') }}</button>
            <button v-if="planStore.featureEnabled('pipeline_delete')" type="button" class="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-1 text-xs font-medium text-destructive hover:bg-destructive/20" @click="showDeleteConfirm = true">{{ $t('common.delete') }}</button>
            <Button severity="secondary" outlined size="small" class="text-xs" @click="addNode">{{ $t('views.PipelineEditorView.add_node') }}</Button>
          </div>
        </div>
        <!-- Toolbar -->
        <div class="absolute left-4 top-4 z-10 flex items-center gap-2 rounded-lg border bg-card px-3 py-2 shadow-sm">
          <div class="flex items-center gap-2">
            <h2 class="text-sm font-semibold">{{ pipeline?.name || $t('views.PipelineEditorView.pipeline_editor') }}</h2>
            <button type="button" class="rounded p-1 hover:bg-accent" @click="openRenameDialog" title="Rename pipeline">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
            </button>
            <span v-if="pipeline?.archived_at" class="rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning">{{ $t('views.PipelineEditorView.archived') }}</span>
            <span v-if="folderPath.length > 0" class="ml-2 flex items-center gap-1 text-xs text-muted-foreground">
              <span v-for="(f, i) in folderPath" :key="f.id">
                <template v-if="i > 0"><span class="text-muted-foreground/50">/</span></template>
                <router-link :to="`/pipelines?folder_id=${f.id}`" class="hover:text-foreground">{{ f.name }}</router-link>
              </span>
            </span>
            <template v-if="linkedLifecycleMaps.length > 0">
              <span class="mx-1 h-3 w-px bg-border" />
              <span class="flex items-center gap-1 text-xs text-muted-foreground">
                <router-link
                  v-for="map in linkedLifecycleMaps"
                  :key="map.id"
                  :to="`/lifecycle-maps/${map.id}`"
                  class="hover:text-foreground"
                >
                  {{ map.name }}
                </router-link>
              </span>
            </template>
          </div>
          <span class="mx-2 h-4 w-px bg-border" />
          <Button size="small" class="text-xs" :disabled="savingGraph" @click="saveGraph" data-testid="pipeline-editor-save">
            <svg v-if="savingGraph" class="mr-1 h-3 w-3 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
            {{ savingGraph ? $t('views.PipelineEditorView.saving_graph') : $t('views.PipelineEditorView.save') }}
          </Button>
          <span v-if="saveGraphError" class="ml-2 text-xs text-destructive" data-testid="pipeline-editor-save-error">{{ saveGraphError }}</span>
          <Button size="small" class="text-xs border-indigo-300 bg-indigo-600 text-white hover:bg-indigo-500" :disabled="running || flowNodes.length === 0" :title="flowNodes.length === 0 ? $t('views.PipelineEditorView.no_nodes_to_run') : ''" @click="openRunDialog" data-testid="pipeline-editor-run">
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor" class="mr-1"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            {{ running ? $t('views.PipelineEditorView.running') : $t('views.PipelineEditorView.run_pipeline') }}
          </Button>
          <div class="relative" @click.stop>
            <button
              type="button"
              class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent"
              @click="showSaveAsDropdown = !showSaveAsDropdown"
            >
              Save as template
            </button>
            <div
              v-if="showSaveAsDropdown"
              class="absolute left-0 top-full mt-1 w-48 rounded-lg border bg-card py-1 shadow-lg"
            >
              <button
                type="button"
                class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent"
                @click="openSaveAsComposite"
              >
                <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 text-indigo-400" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v8M4.93 10.93 12 18l7.07-7.07"/><path d="M4 20h16"/></svg>
                Composite
              </button>
            </div>
          </div>
          <span class="mx-2 h-4 w-px bg-border" />
          <div class="flex items-center gap-1">
            <button v-if="!pipeline?.archived_at" type="button" class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent" @click="handleArchive">{{ $t('views.PipelineEditorView.archive') }}</button>
            <button v-else type="button" class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent" @click="handleUnarchive">{{ $t('views.PipelineEditorView.unarchive') }}</button>
            <button v-if="planStore.featureEnabled('pipeline_delete')" type="button" class="rounded-md border border-destructive/50 bg-destructive/10 px-2 py-1 text-xs font-medium text-destructive hover:bg-destructive/20" @click="showDeleteConfirm = true">{{ $t('common.delete') }}</button>
          </div>
          <span class="mx-2 h-4 w-px bg-border" />
          <div class="flex items-center gap-1">
            <label for="pipeline-max-duration" class="text-[10px] text-muted-foreground whitespace-nowrap">{{ $t('views.PipelineEditorView.max_duration_s') }}:</label>
            <input id="pipeline-max-duration"
              v-model.number="maxDurationInput"
              type="number"
              min="0"
              placeholder="No limit"
              class="w-20 rounded border border-input bg-background px-1.5 py-1 text-xs"
              @change="updateMaxDuration"
              data-testid="pipeline-editor-max-duration"
            />
          </div>
          <span class="mx-2 h-4 w-px bg-border" />
          <div class="relative">
            <button
              type="button"
              ref="retryPolicyToggleRef"
              :id="retryPolicyToggleId"
              class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent flex items-center gap-1"
              @click="toggleRetryPolicy"
              :aria-expanded="retryPolicyOpen"
              aria-haspopup="dialog"
              :aria-controls="retryPolicyPanelId"
              data-testid="pipeline-editor-retry-policy-toggle"
            >
              {{ $t('views.PipelineEditorView.retry_policy') }}
            </button>
            <dialog
              v-if="retryPolicyOpen"
              open
              :id="retryPolicyPanelId"
              ref="retryPolicyPanelRef"
              class="absolute right-0 left-auto top-full z-50 mt-1 w-72 rounded-lg border border-border bg-card p-3 shadow-lg"
              tabindex="-1"
              :aria-label="$t('views.PipelineEditorView.retry_policy')"
              data-testid="pipeline-editor-retry-policy-panel"
            >
              <div class="mb-1 text-xs font-medium text-foreground">{{ $t('views.PipelineEditorView.retry_policy') }}</div>
              <div class="mb-2 text-[10px] text-muted-foreground">
                {{ $t('views.PipelineEditorView.retry_policy_description') }}
              </div>
              <div class="space-y-1">
                <label
                  v-for="opt in retryPolicyOptions"
                  :key="opt.value"
                  class="flex min-h-6 items-center gap-2 text-xs"
                >
                  <input
                    type="checkbox"
                    :value="opt.value"
                    v-model="retryPolicyEvents"
                    class="h-4 w-4"
                    :data-testid="`pipeline-editor-retry-event-${opt.value}`"
                  />
                  {{ $t(opt.labelKey) }}
                </label>
              </div>
              <div class="mt-3 flex items-center gap-2">
                <label for="retry-policy-max" class="text-[10px] text-muted-foreground whitespace-nowrap">
                  {{ $t('views.PipelineEditorView.max_retries') }}
                </label>
                <input
                  id="retry-policy-max"
                  v-model.number="retryPolicyMaxRetries"
                  type="number"
                  min="0"
                  max="5"
                  class="w-14 rounded border border-input bg-background px-1.5 py-1 text-xs"
                  data-testid="pipeline-editor-retry-policy-max"
                />
              </div>
              <div
                v-if="retryPolicyNoRetriesWarning"
                class="mt-2 text-xs text-warning"
                role="alert"
                data-testid="pipeline-editor-retry-policy-warning"
              >
                {{ retryPolicyNoRetriesWarning }}
              </div>
              <div
                v-if="retryPolicyError"
                class="mt-2 text-xs text-destructive"
                role="alert"
                data-testid="pipeline-editor-retry-policy-error"
              >
                {{ retryPolicyError }}
              </div>
              <div class="mt-3 flex justify-end gap-2">
                <button
                  type="button"
                  class="rounded border border-input bg-background px-2 py-1 text-xs hover:bg-accent"
                  @click="closeRetryPolicy"
                >
                  {{ $t('views.PipelineEditorView.cancel') }}
                </button>
                <button
                  type="button"
                  class="rounded border border-input bg-primary px-2 py-1 text-xs text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
                  :disabled="retryPolicySaving"
                  @click="saveRetryPolicy"
                  data-testid="pipeline-editor-retry-policy-save"
                >
                  {{ retryPolicySaving ? $t('views.PipelineEditorView.saving') : $t('views.PipelineEditorView.save') }}
                </button>
              </div>
            </dialog>
          </div>
          <span class="mx-2 h-4 w-px bg-border" />
          <button
            type="button"
            class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent flex items-center gap-1"
            @click="addNode"
            title="Add node"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            Add Node
          </button>
          <span class="mx-2 h-4 w-px bg-border" />
          <button
            type="button"
            class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent flex items-center gap-1"
            @click="() => fitView()"
            title="Fit view"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
            Fit View
          </button>
        </div>
        <!-- Run dialog modal -->
        <div
          v-if="showRunDialog"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          @click.self="closeRunDialog"
        >
          <dialog
            open
            aria-modal="true"
            :aria-label="$t('views.PipelineEditorView.run_dialog_title')"
            class="bg-card border border-border rounded-xl shadow-xl w-full max-w-lg mx-4 p-6 space-y-4"
            style="position: static"
          >
            <div class="flex items-center justify-between">
              <h2 class="text-base font-semibold text-foreground">{{ $t('views.PipelineEditorView.run_dialog_title') }}</h2>
              <button
                type="button"
                class="text-muted-foreground hover:text-foreground transition-colors"
                @click="closeRunDialog"
                aria-label="Close"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
            <p class="text-sm text-muted-foreground">
              Run <span class="font-medium text-foreground">{{ pipeline?.name }}</span>
            </p>
            <div v-if="isWebhookTriggered" class="rounded-lg bg-muted border p-3 text-sm text-muted-foreground">
              {{ $t('views.PipelineEditorView.webhook_triggered_info') }}
            </div>
            <div v-else class="space-y-2">
              <label for="pipeline-editor-run-prompt" class="block text-sm font-medium text-foreground">{{ $t('views.PipelineEditorView.prompt') }}</label>
              <textarea id="pipeline-editor-run-prompt"
                v-model="runPrompt"
                :placeholder="$t('views.PipelineEditorView.run_prompt_placeholder')"
                rows="4"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-primary"
                data-testid="pipeline-editor-run-prompt"
              />
            </div>
            <div v-if="runError" class="rounded-lg bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive">
              {{ runError }}
            </div>
            <div v-if="emptyRunWarning" class="rounded-lg bg-warning/10 border border-warning/30 p-3 text-sm text-warning">
              {{ emptyRunWarning }}
            </div>
            <div class="flex justify-end gap-2 pt-2">
              <button
                type="button"
                class="px-4 py-2 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
                @click="closeRunDialog"
              >
                {{ $t('views.PipelineEditorView.cancel') }}
              </button>
              <Button v-if="!isWebhookTriggered" class="border-indigo-300 bg-indigo-600 text-white hover:bg-indigo-500" :disabled="running" @click="triggerRun" data-testid="pipeline-editor-run-submit">
                <svg
                  v-if="running"
                  class="animate-spin h-4 w-4 mr-1"
                  xmlns="http://www.w3.org/2000/svg"
                  fill="none"
                  viewBox="0 0 24 24"
                >
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                {{ running ? $t('views.PipelineEditorView.running') : $t('views.PipelineEditorView.run_pipeline') }}
              </Button>
            </div>
          </dialog>
        </div>
        <VueFlow
          :key="pipelineId"
          v-model:nodes="flowNodes"
          v-model:edges="flowEdges"
          :node-types="nodeTypes"
          :default-edge-options="{ type: 'smoothstep', animated: false, style: { stroke: '#888' } }"
          :fit-view-on-init="false"
          :source-position="Position.Right"
          :target-position="Position.Left"
          @node-click="onNodeClick"
          @edge-click="onEdgeClick"
          @pane-click="onPaneClick"
        >
          <Background :gap="20" :size="1" />
          <Controls :showInteractive="false" />
          <template #node-manual="nodeProps"><div class="rounded-lg border-2 border-warning/60 bg-warning/10 px-4 py-2 shadow-sm" v-tooltip.top="nodeProps.data.description">
                    <div class="text-xs font-medium text-warning">MANUAL</div>
                    <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
                  </div></template>
          <template #node-agent="nodeProps"><div class="rounded-lg border-2 border-primary/60 bg-primary/10 px-4 py-2 shadow-sm" v-tooltip.top="nodeProps.data.description">
                    <div class="text-xs font-medium text-primary">AGENT</div>
                    <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
                  </div></template>
          <template #edge-default="edgeProps">
            <div v-if="edgeProps.data?.hitl_gate_config" class="absolute -translate-y-4 translate-x-2">
              <span class="rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning">HITL</span>
            </div>
            <div v-if="edgeProps.data?.edge_type === 'loop'" class="absolute translate-y-4 translate-x-2">
              <span class="rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-600 dark:bg-blue-900 dark:text-blue-300">
                Loop{{ edgeProps.data?.max_iterations ? ` (${edgeProps.data.max_iterations})` : '' }}
              </span>
            </div>
            <div v-if="edgeProps.data?.edge_type === 'llm'" class="absolute translate-y-4 translate-x-2">
              <span class="rounded bg-purple-100 px-1.5 py-0.5 text-[10px] font-medium text-purple-600 dark:bg-purple-900 dark:text-purple-300">
                LLM{{ edgeProps.data?.routing_label ? `: ${edgeProps.data.routing_label}` : '' }}
              </span>
            </div>
          </template>
        </VueFlow>
      </div>
      <!-- Node Properties Panel -->
      <aside v-if="selectedNodeData && !selectedEdgeData" class="w-96 overflow-y-auto border-l bg-card p-4">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.PipelineEditorView.node_properties') }}</h2>
        <dl class="space-y-4 text-sm">
          <div>
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">ID</dt>
            <dd class="font-mono text-[10px] text-muted-foreground break-all select-all" :title="selectedNodeData.id">{{ shortId(selectedNodeData.id) }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.type_label') }}</dt>
            <dd>
              <span
                :class="selectedNodeData.node_type === 'manual'
                  ? 'badge badge-status-warning'
                  : 'badge badge-status-primary'"
              >
                {{ selectedNodeData.node_type === 'manual' ? $t('views.PipelineEditorView.manual') : selectedNodeData.node_type === 'sandbox_agent' ? 'Sandbox Agent' : $t('views.PipelineEditorView.agent') }}
              </span>
            </dd>
          </div>
          <div>
            <label for="pipeline-editor-node-label" class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.label') }}</label>
            <input id="pipeline-editor-node-label"
              v-model="selectedNodeData.label"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium mt-1"
              placeholder="Enter node label"
              @input="syncNodeToFlow"
              data-testid="pipeline-editor-node-label"
            />
          </div>
          <div>
            <label for="pipeline-editor-node-desc" class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.description') }}</label>
            <textarea id="pipeline-editor-node-desc"
              v-model="selectedNodeData.description"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm mt-1"
              placeholder="Optional description"
              rows="2"
              @input="syncNodeToFlow"
              data-testid="pipeline-editor-node-description"
            />
          </div>
          <!-- FAR-295: retry-safety indicator for every node type -->
          <div>
            <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-idempotent-label">{{ $t('views.PipelineEditorView.idempotent') }}</dt>
            <dd data-testid="pipeline-editor-idempotent-value">
              {{ selectedNodeData.idempotent === false ? $t('views.PipelineEditorView.disabled') : $t('views.PipelineEditorView.enabled') }}
            </dd>
            <p class="mt-0.5 text-[11px] text-muted-foreground">{{ $t('views.PipelineEditorView.idempotent_description') }}</p>
          </div>
          <!-- Manual node: Output Schema -->
          <div v-if="selectedNodeData.node_type === 'manual' && selectedNodeData.output_schema_id">
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.output_schema') }}</dt>
            <dd class="font-medium">{{ schemaName(selectedNodeData.output_schema_id) || shortId(selectedNodeData.output_schema_id) }}</dd>
          </div>
          <!-- Agent node: Agent details -->
          <template v-if="(selectedNodeData.node_type === 'agent' || selectedNodeData.node_type === 'sandbox_agent') && selectedNodeData.agent_id">
            <div>
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.agent') }}</dt>
              <dd class="font-medium">{{ agentName(selectedNodeData.agent_id) || shortId(selectedNodeData.agent_id) }}</dd>
              <router-link
                v-if="selectedNodeData.agent_id"
                :to="`/admin/agents/${selectedNodeData.agent_id}`"
                class="mt-0.5 inline-flex items-center gap-1 text-xs text-indigo-500 hover:text-indigo-400"
              >
                {{ $t('views.PipelineEditorView.view_agent') }}
                <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
              </router-link>
            </div>
            <div v-if="agentModelBackendId(selectedNodeData.agent_id)">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.model_backend') }}</dt>
              <dd class="font-medium">{{ agentModelBackendName(selectedNodeData.agent_id) || shortId(agentModelBackendId(selectedNodeData.agent_id)) }}</dd>
            </div>
            <div v-if="agentInputSchemaId(selectedNodeData.agent_id)">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.input_schema') }}</dt>
              <dd class="font-medium">{{ schemaName(agentInputSchemaId(selectedNodeData.agent_id) ?? '') || shortId(agentInputSchemaId(selectedNodeData.agent_id) ?? '') }}</dd>
            </div>
            <div v-if="agentOutputSchemaId(selectedNodeData.agent_id)">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.output_schema') }}</dt>
              <dd class="font-medium">{{ schemaName(agentOutputSchemaId(selectedNodeData.agent_id) ?? '') || shortId(agentOutputSchemaId(selectedNodeData.agent_id) ?? '') }}</dd>
            </div>
            <div v-if="selectedNodeData.connector_binding">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.connector') }}</dt>
              <dd class="font-medium">{{ connectorName(selectedNodeData.connector_binding) }}</dd>
            </div>
            <!-- Parameter Schema + Set -->
            <div v-if="agentParamSchema(selectedNodeData.agent_id)">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.param_schema') }}</dt>
              <dd class="font-medium">{{ agentParamSchemaName(selectedNodeData.agent_id) }}</dd>
            </div>
            <div v-if="agentParamSchema(selectedNodeData.agent_id)">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.param_set') }}</dt>
              <dd>
                <select
                  v-model="selectedNodeParamSetId"
                  :aria-label="$t('views.PipelineEditorView.param_set')"
                  class="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-sm"
                  @change="onParamSetChange"
                  data-testid="pipeline-node-param-set"
                >
                  <option :value="undefined">{{ $t('views.PipelineEditorView.no_set') }}</option>
                  <option
                    v-for="ps in availableParamSets"
                    :key="ps.id"
                    :value="ps.id"
                  >{{ ps.name }}</option>
                </select>
              </dd>
            </div>
            <div v-if="selectedNodeParamSetId && paramSetOverridesKeys.length > 0">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.overrides') }}</dt>
              <dd class="space-y-2">
                <div v-for="pkey in paramSetOverridesKeys" :key="pkey" class="flex flex-col gap-0.5">
                  <label :for="'pipelineeditorview-override-' + pkey" class="text-xs text-muted-foreground">{{ paramDefLabel(pkey) }}</label>
                  <textarea
                    v-if="paramDefByKey(pkey)?.type === 'string' && paramDefByKey(pkey)?.multiline"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                    rows="2"
                  />
                  <input
                    v-else-if="paramDefByKey(pkey)?.type === 'string'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    type="text"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  />
                  <input
                    v-else-if="paramDefByKey(pkey)?.type === 'number'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model.number="selectedNodeOverrides[pkey]"
                    type="number"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  />
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'boolean'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option :value="true">true</option>
                    <option :value="false">false</option>
                  </select>
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'select'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option v-for="o in (paramDefByKey(pkey)?.options || [])" :key="o" :value="o">{{ o }}</option>
                  </select>
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'model_backend_ref'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option v-for="mb in modelBackends" :key="mb.id" :value="mb.id">{{ mb.display_name || mb.name || shortId(mb.id) }}</option>
                  </select>
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'schema_ref'"
                    :id="'pipelineeditorview-override-' + pkey"
                    v-model="selectedNodeOverrides[pkey]"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option v-for="s in schemas" :key="s.id" :value="s.id">{{ s.name || shortId(s.id) }}</option>
                  </select>
                  <span v-else class="text-xs text-muted-foreground">—</span>
                </div>
                <button
                  type="button"
                  class="mt-1 text-xs text-indigo-500 hover:text-indigo-400"
                  data-testid="pipeline-save-param-set"
                  @click="saveAsNewParamSet"
                >
                  {{ $t('views.PipelineEditorView.save_as_new_set') }}
                </button>
              </dd>
            </div>
          </template>
          <!-- Sandbox Agent: template, command, env, context -->
          <template v-if="selectedNodeData.node_type === 'sandbox_agent'">
            <div v-if="selectedNodeData.template_id">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.template') }}</dt>
              <dd class="font-mono text-xs select-all" :title="selectedNodeData.template_id">{{ shortId(selectedNodeData.template_id) }}</dd>
            </div>
            <div v-if="selectedNodeData.agent_command">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.command') }}</dt>
              <dd class="font-mono text-xs break-all">{{ selectedNodeData.agent_command }}</dd>
            </div>
            <template v-else-if="selectedNodeData.agent_commands && selectedNodeData.agent_commands.length > 0">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.commands') }}</dt>
              <dd>
                <ul class="list-inside list-decimal text-xs font-mono text-muted-foreground">
                  <li v-for="(cmd, idx) in selectedNodeData.agent_commands" :key="idx">{{ cmd }}</li>
                </ul>
                <div v-if="selectedNodeData.commands_concatenation_string" class="text-[10px] text-muted-foreground mt-1">
                  Concatenated with: <code class="font-mono">{{ selectedNodeData.commands_concatenation_string }}</code>
                </div>
              </dd>
            </template>
            <div v-if="selectedNodeData.timeout_seconds">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.timeout') }}</dt>
              <dd>{{ selectedNodeData.timeout_seconds }}s</dd>
            </div>
            <div v-if="selectedNodeData.stall_timeout_seconds">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-stall-timeout-label">{{ $t('views.PipelineEditorView.stall_timeout') }}</dt>
              <dd data-testid="pipeline-editor-stall-timeout-value">{{ selectedNodeData.stall_timeout_seconds }}s</dd>
            </div>
            <div>
              <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-heartbeat-label">{{ $t('views.PipelineEditorView.heartbeat') }}</dt>
              <dd data-testid="pipeline-editor-heartbeat-value">{{ selectedNodeData.enable_heartbeat === false ? $t('views.PipelineEditorView.disabled') : $t('views.PipelineEditorView.enabled') }}</dd>
            </div>
            <div v-if="selectedNodeData.watch_log_path">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-watch-log-path-label">{{ $t('views.PipelineEditorView.watch_log_path') }}</dt>
              <dd class="font-mono text-xs break-all" data-testid="pipeline-editor-watch-log-path-value">{{ selectedNodeData.watch_log_path }}</dd>
            </div>
            <div v-if="selectedNodeData.stdout_percentage_delta != null">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-stdout-delta-label">{{ $t('views.PipelineEditorView.stdout_delta') }}</dt>
              <dd data-testid="pipeline-editor-stdout-delta-value">{{ selectedNodeData.stdout_percentage_delta }}</dd>
            </div>
            <div v-if="selectedNodeData.watch_globs && selectedNodeData.watch_globs.length > 0">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-watch-globs-label">{{ $t('views.PipelineEditorView.watch_globs') }}</dt>
              <dd class="font-mono text-[10px] break-all" data-testid="pipeline-editor-watch-globs-value">{{ selectedNodeData.watch_globs.join(', ') }}</dd>
            </div>
            <div v-if="selectedNodeData.env_vars && Object.keys(selectedNodeData.env_vars).length > 0">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.env_vars') }}</dt>
              <dd class="font-mono text-[10px] break-all">{{ Object.keys(selectedNodeData.env_vars).join(', ') }}</dd>
            </div>
            <div v-if="selectedNodeData.context_files && Object.keys(selectedNodeData.context_files).length > 0">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.context_files') }}</dt>
              <dd><ul class="list-inside list-disc text-xs text-muted-foreground"><li v-for="(content, fpath) in selectedNodeData.context_files" :key="fpath">{{ fpath }} <span class="text-[10px] opacity-60">({{ content.length }} bytes)</span></li></ul></dd>
            </div>
            <div v-if="selectedNodeData.agent_prompt">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.prompt') }}</dt>
              <dd class="text-xs text-muted-foreground italic whitespace-pre-wrap max-h-32 overflow-y-auto">{{ selectedNodeData.agent_prompt.substring(0, 300) }}{{ selectedNodeData.agent_prompt.length > 300 ? '...' : '' }}</dd>
            </div>
          </template>
          <!-- Lifecycle maps -->
          <div v-if="linkedLifecycleMaps.length > 0">
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.lifecycle_maps') }}</dt>
            <dd>
              <div v-for="map in linkedLifecycleMaps" :key="map.id" class="flex items-center gap-1">
                <router-link :to="`/lifecycle-maps/${map.id}`" class="text-xs text-indigo-500 hover:text-indigo-400">
                  {{ map.name }}
                </router-link>
              </div>
            </dd>
          </div>
        </dl>
        <div class="mt-6 space-y-2">
          <Button v-if="selectedNodeData.node_type === 'manual'" class="w-full" data-testid="pipeline-editor-convert-to-agent" @click="openAgentPicker">
            Convert to Agent
          </Button>
          <button
            v-if="selectedNodeData.node_type === 'agent'"
            type="button"
            data-testid="pipeline-editor-revert-to-manual"
            class="inline-flex w-full items-center justify-center rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            @click="openRevertDialog"
          >
            {{ $t('views.PipelineEditorView.revert_to_manual') }}
          </button>
        </div>
      </aside>
      <!-- Edge Properties Panel (with HITL gate config) -->
      <aside v-if="selectedEdgeData" class="w-96 overflow-y-auto border-l bg-card p-4">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.PipelineEditorView.edge_properties') }}</h2>
        <dl class="space-y-3 text-sm">
          <div>
            <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.source') }}</dt>
            <dd class="font-mono text-xs">{{ shortId(selectedEdgeData.source_node_id) }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.target') }}</dt>
            <dd class="font-mono text-xs">{{ shortId(selectedEdgeData.target_node_id) }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.type_label') }}</dt>
            <dd>
              <Select
  aria-label="Edge type"
  v-model="edgeForm.edge_type"
  placeholder="Normal"
  class="w-full"
  :options="[{ value: 'normal', label: $t('views.PipelineEditorView.normal') }, { value: 'reject', label: $t('views.PipelineEditorView.reject') }, { value: 'conditional', label: $t('views.PipelineEditorView.conditional') }, { value: 'loop', label: $t('views.PipelineEditorView.loop') }, { value: 'llm', label: $t('views.PipelineEditorView.llm_routing') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
            </dd>
          </div>
          <div>
            <label for="pipelineeditorview-field-16" class="text-muted-foreground">{{ $t('views.PipelineEditorView.condition_expression') }}</label>
            <dd>
              <input
                id="pipelineeditorview-field-16"
                v-model="edgeForm.condition_expression"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                placeholder="JMESPath expression (e.g. score > `0.5`)"
                :disabled="edgeForm.edge_type !== 'conditional' && edgeForm.edge_type !== 'loop'"
              />
            </dd>
          </div>
          <div v-if="edgeForm.edge_type === 'loop'">
            <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.max_iterations') }}</dt>
            <dd>
              <input
                v-model.number="edgeForm.max_iterations"
                type="number"
                min="0"
                :aria-label="$t('views.PipelineEditorView.max_iterations')"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="0 = unlimited (RunawayGuard applies)"
              />
              <p class="mt-1 text-xs text-muted-foreground">Maximum number of times this loop can repeat before exiting. 0 means no limit.</p>
            </dd>
          </div>
          <div v-if="edgeForm.edge_type === 'llm'">
            <label for="pipelineeditorview-field-17" class="text-muted-foreground">{{ $t('views.PipelineEditorView.routing_label') }}</label>
            <dd>
              <input
                id="pipelineeditorview-field-17"
                v-model="edgeForm.routing_label"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                placeholder="e.g. retry, escalate, complete"
              />
              <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.PipelineEditorView.the_llm_uses_this_label_to_select_this_path_must_be_unique_among_outgoing_edges') }}</p>
            </dd>
          </div>
        </dl>
        <hr class="my-4 border-t" />
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-semibold">HITL Gate</h3>
          <label for="pipelineeditorview-field-15" class="inline-flex cursor-pointer items-center">
            <input id="pipelineeditorview-field-15"
              v-model="edgeForm.hitl_enabled"
              type="checkbox"
              class="h-4 w-4 rounded border-gray-300 text-primary focus:ring-primary"
            />
            <span class="ml-2 text-xs text-muted-foreground">{{ $t('views.PipelineEditorView.enabled') }}</span>
          </label>
        </div>
        <div v-if="edgeForm.hitl_enabled" class="mt-4 space-y-4">
          <div>
            <label for="pipelineeditorview-field-14" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.label') }}</label>
            <input id="pipelineeditorview-field-14"
              v-model="edgeForm.label"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="e.g. Review before deploy"
            />
          </div>
          <div>
            <label for="pipelineeditorview-field-13" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.description') }}</label>
            <textarea id="pipelineeditorview-field-13"
              v-model="edgeForm.description"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="Describe what the reviewer should check"
              rows="2"
            />
          </div>
          <div>
            <label for="pipelineeditorview-field-12" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.claim_expiry_minutes') }}</label>
            <input id="pipelineeditorview-field-12"
              v-model.number="edgeForm.claim_expiry_minutes"
              type="number"
              min="1"
              max="1440"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            />
          </div>
          <div class="flex items-center gap-2">
            <input aria-label="checkbox"
              v-model="edgeForm.human_only"
              type="checkbox"
              class="h-4 w-4 rounded border-gray-300 text-primary focus:ring-primary"
            />
            <span class="text-xs text-muted-foreground">{{ $t('views.PipelineEditorView.human_only_block_llm_auto_approval') }}</span>
          </div>
          <hr class="border-t" />
          <div>
            <label for="pipelineeditorview-field-11" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.condition_type') }}</label>
            <Select
  aria-label="Condition type"
  v-model="edgeForm.condition_type"
  placeholder="None (always gate)"
  class="w-full"
  :options="[{ value: 'none', label: $t('views.PipelineEditorView.none_always_gate') }, { value: 'jmespath', label: $t('views.PipelineEditorView.jmespath_expression') }, { value: 'eval', label: $t('views.PipelineEditorView.eval_reference') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <div v-if="edgeForm.condition_type === 'jmespath'">
            <label for="pipelineeditorview-field-10" class="mb-1 block text-xs font-medium text-muted-foreground">JMESPath Condition</label>
            <input id="pipelineeditorview-field-10"
              v-model="edgeForm.condition"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
              placeholder="e.g. score > `0.5`"
            />
            <p class="mt-1 text-[10px] text-muted-foreground">
              Evaluated against pipeline state. If truthy, gate activates.
            </p>
          </div>
          <div v-if="edgeForm.condition_type === 'eval'" class="space-y-3">
            <div>
              <label for="pipelineeditorview-field-9" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.eval_name') }}</label>
              <input id="pipelineeditorview-field-9"
                v-model="edgeForm.eval_name"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                placeholder="e.g. quality-check"
              />
            </div>
            <div class="flex gap-2">
              <div class="flex-1">
                <label for="pipelineeditorview-field-8" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.threshold') }}</label>
                <input id="pipelineeditorview-field-8"
                  v-model.number="edgeForm.eval_threshold"
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                />
              </div>
              <div class="flex-1">
                <label for="pipelineeditorview-field-7" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.PipelineEditorView.operator') }}</label>
                <Select
  aria-label="Operator"
  v-model="edgeForm.eval_operator"
  placeholder="lt (score &lt; threshold)"
  class="w-full"
  :options="[{ value: 'lt', label: 'lt (score < threshold)' }, { value: 'gt', label: 'gt (score > threshold)' }, { value: 'lte', label: 'lte (score ≤ threshold)' }, { value: 'gte', label: 'gte (score ≥ threshold)' }, { value: 'eq', label: 'eq (score == threshold)' }, { value: 'neq', label: 'neq (score != threshold)' }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
              </div>
            </div>
            <p class="mt-1 text-[10px] text-muted-foreground">
              If condition is true, gate fires. If false, gate is skipped.
            </p>
          </div>
          <div class="flex gap-2 pt-2">
            <Button data-testid="pipeline-editor-save-edge" class="flex-1" :disabled="savingEdge" @click="saveEdgeConfig">
              {{ savingEdge ? 'Saving...' : 'Save Edge' }}
            </Button>
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="selectedEdgeData = null"
            >
              Close
            </button>
          </div>
          <div v-if="edgeSaveError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ edgeSaveError }}
          </div>
        </div>
      </aside>
    </template>
    <FormDialog
      :open="showAgentPicker"
      @update:open="showAgentPicker = false"
      :title="$t('views.PipelineEditorView.convert_to_agent')"
      confirmText="Convert"
      :confirmDisabled="!canConvert"
      @confirm="convertToAgent"
    >
      <div class="space-y-4">
          <div>
            <label for="pipelineeditorview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.agent') }}</label>
            <Select
  aria-label="Agent"
  v-model="pickerAgentId"
  @update:model-value="onAgentChange"
  :placeholder="$t('views.PipelineEditorView.select_agent_placeholder')"
  data-testid="pipeline-editor-agent-select"
  class="w-full"
  :options="[{ value: '__all__', label: $t('common.none') }, ...agents.map(a => ({ value: a.id, label: a.name }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <div v-if="selectedAgent">
            <label for="pipelineeditorview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.connector') }}</label>
            <Select
  aria-label="Connector"
  v-model="pickerConnectorId"
  :placeholder="$t('views.PipelineEditorView.select_connector_placeholder')"
  data-testid="pipeline-editor-connector-select"
  class="w-full"
  :options="[{ value: '__all__', label: $t('common.none') }, ...eligibleConnectors.map(c => ({ value: c.id, label: c.name + '(' + c.connector_type_id + ')' }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <div v-if="selectedAgent">
            <span class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.model_backend_label') }}</span>
            <div class="rounded-lg border bg-muted px-3 py-2 text-sm">
              {{ modelBackendName || $t('views.PipelineEditorView.loading') }}
            </div>
          </div>
          <div v-if="selectedAgent" class="rounded-lg border bg-muted p-3 text-sm">
            <p class="text-xs text-muted-foreground">{{ $t('views.PipelineEditorView.schema') }}</p>
            <p class="mt-0.5 font-medium">Input: {{ agentSchemaName(selectedAgent, 'input') }}</p>
            <p class="font-medium">Output: {{ agentSchemaName(selectedAgent, 'output') }}</p>
          </div>
          <div v-if="convertError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ convertError }}
          </div>
      </div>
    </FormDialog>
    <FormDialog
      :open="showRevertDialog"
      @update:open="showRevertDialog = false"
      :title="$t('views.PipelineEditorView.revert_dialog_title')"
      confirmText="Revert"
      :confirmDisabled="!revertSnapshotId"
      @confirm="revertToManual"
    >
      <div v-if="revertLoading" class="flex items-center justify-center py-8">
        <div class="h-6 w-6 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>
      <div v-else class="space-y-4">
        <p class="text-sm text-muted-foreground">
          {{ $t('views.PipelineEditorView.select_snapshot_description') }}
        </p>
        <div>
          <label for="pipelineeditorview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.snapshot_label') }}</label>
          <Select
  aria-label="Snapshot"
  v-model="revertSnapshotId"
  :placeholder="$t('views.PipelineEditorView.select_snapshot_placeholder')"
  data-testid="pipeline-editor-snapshot-select"
  class="w-full"
  :options="[{ value: '__all__', label: $t('common.none') }, ...snapshots.map(s => ({ value: s.id, label: 'v' + s.snapshot_version + (s.tag ? ` — ${s.tag}` : '') }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
        <div v-if="revertError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          {{ revertError }}
        </div>
      </div>
    </FormDialog>
    <FormDialog
      :open="showSaveAsComposite"
      @update:open="showSaveAsComposite = false"
      title="Save as Composite"
      confirmText="Save"
      :confirmDisabled="!saveAsName || saveAsSelectedNodeIds.length === 0 || saving"
      :loading="saving"
      @confirm="handleSaveAsComposite"
    >
      <p class="mb-4 text-sm text-muted-foreground">
        Extracts selected nodes from this pipeline into a reusable composite template.
        Parameter placeholders (&#123;&#123;parameter.*&#125;&#125;) in agent prompts are auto-detected.
      </p>
      <div class="space-y-4">
        <div>
          <label for="pipelineeditorview-field-3" class="mb-1 block text-sm font-medium">Name *</label>
          <input id="pipelineeditorview-field-3"
            v-model="saveAsName"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            placeholder="My Composite"
          />
        </div>
        <div>
          <label for="pipelineeditorview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.description') }}</label>
          <textarea id="pipelineeditorview-field-2"
            v-model="saveAsDescription"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            rows="3"
            placeholder="Optional description"
          />
        </div>
        <div>
          <span class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.selected_nodes') }}</span>
          <div class="max-h-32 space-y-1 overflow-y-auto">
            <label
              v-for="node in rawNodes"
              :key="node.id"
              class="flex items-center gap-2 rounded-md bg-muted/30 px-3 py-1.5 text-sm"
            >
              <input
                v-model="saveAsSelectedNodeIds"
                type="checkbox"
                :value="node.id"
                class="h-4 w-4 rounded border-gray-300 text-indigo-500 focus:ring-indigo-500"
              />
              <span>{{ node.label || 'Node ' + shortId(node.id) }}</span>
            </label>
          </div>
        </div>
        <div v-if="saveAsError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          {{ saveAsError }}
        </div>
      </div>
    </FormDialog>
    <FormDialog
      :open="showRenameDialog"
      @update:open="showRenameDialog = false"
      title="Rename Pipeline"
      confirmText="Save"
      :confirmDisabled="!renameName.trim() || renaming"
      :loading="renaming"
      @confirm="handleRename"
    >
      <div class="space-y-4">
        <div>
          <label for="pipelineeditorview-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.name') }}</label>
          <input id="pipelineeditorview-field-1"
            v-model="renameName"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            placeholder="Pipeline name"
            @keyup.enter="handleRename"
          />
        </div>
        <div v-if="renameError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          {{ renameError }}
        </div>
      </div>
    </FormDialog>
    <FormDialog
      :open="showDeleteConfirm"
      @update:open="showDeleteConfirm = false"
      title="Delete Pipeline"
      confirmText="Delete"
      @confirm="handleDelete"
    >
      <p class="mb-4 text-sm text-muted-foreground">
        Are you sure? This permanently deletes the pipeline and all its runs.
      </p>
      <div v-if="deleteError" class="mb-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
        {{ deleteError }}
      </div>
    </FormDialog>
  </div>
</template>
<script setup lang="ts">
import { ref, computed, reactive, watch, nextTick, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { VueFlow, useVueFlow, Position } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { usePlanStore } from '../stores/planStore'

import FormDialog from '../components/shared/FormDialog.vue'
import { shortId } from '../utils/format'
import { api } from '../lib/api/client'
import { useApi } from '../composables/useApi'
import Button from 'primevue/button'
import Select from 'primevue/select'

function withTimeout<T>(factory: (signal: AbortSignal) => Promise<T>, ms = 15000): Promise<T> {
  const ctrl = new AbortController()
  const timeout = setTimeout(() => ctrl.abort(), ms)
  return factory(ctrl.signal).finally(() => clearTimeout(timeout))
}

const { t } = useI18n()
const planStore = usePlanStore()
const route = useRoute()
const router = useRouter()
const pipelineId = route.params.id as string

const rawNodes = ref<any[]>([])
const rawEdges = ref<any[]>([])
const flowNodes = ref<any[]>([])
const flowEdges = ref<any[]>([])

const selectedNodeData = ref<any | null>(null)
const selectedEdgeData = ref<any | null>(null)
const showSaveAsDropdown = ref(false)
const nodeTypes = { agent: 'agent', manual: 'manual' }
const { fitView } = useVueFlow()

const agents = ref<any[]>([])
const connectors = ref<any[]>([])
const modelBackends = ref<any[]>([])
const schemas = ref<any[]>([])
const snapshots = ref<any[]>([])

const showAgentPicker = ref(false)
const showRevertDialog = ref(false)
const showSaveAsComposite = ref(false)
const pickerAgentId = ref<string>('__all__')
const pickerConnectorId = ref<string>('__all__')
const revertSnapshotId = ref<string>('__all__')
const convertError = ref<string | null>(null)
const { get, post: postUntyped } = useApi()
const revertError = ref<string | null>(null)
const revertLoading = ref(false)

const saveAsName = ref('')
const saveAsDescription = ref('')
const saveAsSelectedNodeIds = ref<string[]>([])
const saveAsError = ref<string | null>(null)
const saving = ref(false)

const savingEdge = ref(false)
const edgeSaveError = ref<string | null>(null)

const pipeline = ref<any>(null)
const showRenameDialog = ref(false)
const renameName = ref('')
const renameError = ref<string | null>(null)
const renaming = ref(false)
const showDeleteConfirm = ref(false)
const deleteError = ref<string | null>(null)

const savingGraph = ref(false)
const saveGraphError = ref<string | null>(null)
const showRunDialog = ref(false)
const runPrompt = ref('')
const running = ref(false)
const runError = ref<string | null>(null)
const confirmEmptyRun = ref(false)
const emptyRunWarning = ref<string | null>(null)

watch(runPrompt, () => {
  if (confirmEmptyRun.value) {
    confirmEmptyRun.value = false
    emptyRunWarning.value = null
  }
})

const maxDurationInput = ref<number | undefined>(undefined)

const retryPolicyOpen = ref(false)
const retryPolicySaving = ref(false)
const retryPolicyEvents = ref<string[]>([])
const retryPolicyMaxRetries = ref(0)
const retryPolicyError = ref<string | null>(null)
const retryPolicyToggleRef = ref<HTMLButtonElement | null>(null)
const retryPolicyPanelRef = ref<HTMLElement | null>(null)
const retryPolicyToggleId = 'pipeline-editor-retry-policy-toggle'
const retryPolicyPanelId = 'pipeline-editor-retry-policy-panel'
const retryPolicyOptions = [
  { value: 'stall', labelKey: 'views.PipelineEditorView.retry_policy_stall' },
  { value: 'timeout', labelKey: 'views.PipelineEditorView.retry_policy_timeout' },
  { value: 'failure', labelKey: 'views.PipelineEditorView.retry_policy_failure' },
]

interface RetryPolicy {
  on?: string[]
  max_retries?: number
  [key: string]: unknown
}

type PipelineRetryPolicySource = {
  retry_policy?: RetryPolicy | null
}

const retryPolicyNoRetriesWarning = computed(() => {
  if (retryPolicyEvents.value.length > 0 && (Number(retryPolicyMaxRetries.value) || 0) === 0) {
    return t('views.PipelineEditorView.retry_policy_warning_no_max')
  }
  return null
})

function syncRetryPolicyFromPipeline() {
  const rp = (pipeline.value as PipelineRetryPolicySource | null)?.retry_policy
  if (rp && typeof rp === 'object' && !Array.isArray(rp)) {
    const events = Array.isArray(rp.on)
      ? rp.on.filter((e: string): e is string => ['stall', 'timeout', 'failure'].includes(e))
      : []
    retryPolicyEvents.value = events
    const max = typeof rp.max_retries === 'number' ? Math.round(rp.max_retries) : 0
    retryPolicyMaxRetries.value = Math.min(5, Math.max(0, max))
  } else {
    retryPolicyEvents.value = []
    retryPolicyMaxRetries.value = 0
  }
  retryPolicyError.value = null
}

function toggleRetryPolicy() {
  retryPolicyOpen.value = !retryPolicyOpen.value
  if (retryPolicyOpen.value) {
    syncRetryPolicyFromPipeline()
    nextTick(() => {
      retryPolicyPanelRef.value?.focus()
    })
  } else {
    retryPolicyToggleRef.value?.focus()
  }
}

function closeRetryPolicy() {
  retryPolicyOpen.value = false
  retryPolicyToggleRef.value?.focus()
}

function onRetryPolicyKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && retryPolicyOpen.value) {
    event.preventDefault()
    closeRetryPolicy()
  }
}

watch(retryPolicyOpen, (open) => {
  if (open) {
    document.addEventListener('keydown', onRetryPolicyKeydown)
  } else {
    document.removeEventListener('keydown', onRetryPolicyKeydown)
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onRetryPolicyKeydown)
})

async function saveRetryPolicy() {
  if (retryPolicySaving.value) return
  retryPolicyError.value = null
  const max = Math.min(5, Math.max(0, Number(retryPolicyMaxRetries.value) || 0))
  const on = [...retryPolicyEvents.value]
  if (on.length > 0 && max === 0) {
    retryPolicyError.value = t('views.PipelineEditorView.retry_policy_warning_no_max')
    return
  }
  const body: { retry_policy: RetryPolicy } = {
    retry_policy: on.length > 0 ? { on, max_retries: max } : {},
  }
  retryPolicySaving.value = true
  try {
    await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      body,
      signal,
    }))
    await loadPipeline()
    retryPolicyOpen.value = false
    retryPolicyToggleRef.value?.focus()
    saveGraphError.value = null
  } catch (e) {
    retryPolicyError.value = `${t('views.PipelineEditorView.retry_policy_update_failed')}${formatApiError(e)}`
  } finally {
    retryPolicySaving.value = false
  }
}

const folders = ref<any[]>([])
const linkedLifecycleMaps = ref<any[]>([])

const folderPath = computed(() => {
  const path: { name: string; id: string }[] = []
  let current: any = folders.value.find((f: any) => f.id === pipeline.value?.folder_id)
  while (current) {
    path.unshift({ name: current.name, id: current.id })
    current = current.parent_id ? folders.value.find((f: any) => f.id === current.parent_id) : null
  }
  return path
})

const defaultEdgeForm = {
  edge_type: 'normal',
  condition_expression: '',
  max_iterations: 0,
  routing_label: '',
  hitl_enabled: false,
  label: '',
  description: '',
  claim_expiry_minutes: 15,
  human_only: false,
  condition_type: 'none',
  condition: '',
  eval_name: '',
  eval_threshold: 0.8,
  eval_operator: 'lt',
}

const edgeForm = reactive({ ...defaultEdgeForm })

const selectedAgent = computed(() => agents.value.find(a => a.id === pickerAgentId.value) || null)

const eligibleConnectors = computed(() => {
  if (!selectedAgent.value) return []
  const refs: Array<{ connector_type: string }> = selectedAgent.value.connector_type_refs || []
  const allowedTypes = new Set(refs.map(r => r.connector_type))
  return connectors.value.filter(c => allowedTypes.has(c.connector_type_id))
})

const modelBackendName = computed(() => {
  if (!selectedAgent.value) return ''
  const mb = modelBackends.value.find(b => b.id === selectedAgent.value.model_backend_id)
  return mb ? `${mb.display_name} (${mb.provider})` : 'Unknown'
})

function agentSchemaName(agent: any, dir: 'input' | 'output') {
  const s = schemas.value.find(s => s.id === agent[`${dir}_schema_id`])
  return s ? s.name : `${dir}_schema_id`
}

const isWebhookTriggered = computed(() => pipeline.value?.trigger_type === 'webhook')

function agentName(agentId: string): string | undefined {
  return agents.value.find((a: any) => a.id === agentId)?.name
}

function agentModelBackendId(agentId: string): string | undefined {
  const agent = agents.value.find((a: any) => a.id === agentId)
  return agent?.model_backend_id
}

function agentModelBackendName(agentId: string): string | undefined {
  const agent = agents.value.find((a: any) => a.id === agentId)
  if (!agent?.model_backend_id) return undefined
  const mb = modelBackends.value.find((b: any) => b.id === agent.model_backend_id)
  return mb?.display_name
}

function agentInputSchemaId(agentId: string): string | undefined {
  const agent = agents.value.find((a: any) => a.id === agentId)
  return agent?.input_schema_id
}

function agentOutputSchemaId(agentId: string): string | undefined {
  const agent = agents.value.find((a: any) => a.id === agentId)
  return agent?.output_schema_id
}

function schemaName(schemaId: string): string | undefined {
  const s = schemas.value.find((s: any) => s.id === schemaId)
  return s?.name
}

function connectorName(binding: any): string {
  if (!binding) return '-'
  const conn = connectors.value.find((c: any) => c.id === binding.instance_id)
  if (conn) return `${conn.name} (${binding.type})`
  return binding.instance_id ? `${binding.type} / ${shortId(binding.instance_id)}` : binding.type
}

function syncNodeToFlow() {
  if (!selectedNodeData.value) return
  const fn = flowNodes.value.find((n: any) => n.id === selectedNodeData.value.id)
  if (fn) {
    fn.data = { ...fn.data, label: selectedNodeData.value.label, description: selectedNodeData.value.description || '' }
  }
}

// Parameter schema + set support
const paramSchemas = ref<any[]>([])
const paramSets = ref<any[]>([])
const selectedNodeParamSetId = ref<string | undefined>(undefined)
const selectedNodeOverrides = ref<Record<string, any>>({})

function agentParamSchema(agentId: string): any | undefined {
  const agent = agents.value.find((a: any) => a.id === agentId)
  if (!agent?.parameter_schema_id) return undefined
  return paramSchemas.value.find((ps: any) => ps.id === agent.parameter_schema_id)
}

function agentParamSchemaName(agentId: string): string | undefined {
  return agentParamSchema(agentId)?.name
}

const availableParamSets = computed(() => {
  const schema = agentParamSchema(selectedNodeData.value?.agent_id)
  if (!schema) return []
  return paramSets.value.filter((ps: any) => ps.parameter_schema_id === schema.id)
})

const paramSetOverridesKeys = computed(() => Object.keys(selectedNodeOverrides.value))

function paramDefByKey(key: string): any | undefined {
  const schema = agentParamSchema(selectedNodeData.value?.agent_id)
  return schema?.parameters?.find((p: any) => p.name === key)
}

function paramDefLabel(key: string): string {
  const def = paramDefByKey(key)
  return def?.label || def?.name || key
}

function onParamSetChange() {
  if (!selectedNodeParamSetId.value) {
    selectedNodeOverrides.value = {}
    return
  }
  const set = paramSets.value.find((ps: any) => ps.id === selectedNodeParamSetId.value)
  selectedNodeOverrides.value = { ...(set?.values ?? {}) }
  // Also update the backend node data
  if (selectedNodeData.value) {
    selectedNodeData.value.parameter_set_id = selectedNodeParamSetId.value
    selectedNodeData.value.parameter_overrides = { ...selectedNodeOverrides.value }
  }
}

async function saveAsNewParamSet() {
  const schema = agentParamSchema(selectedNodeData.value?.agent_id)
  if (!schema) return
  const name = prompt('Name for new parameter set:')
  if (!name?.trim()) return
  try {
    const resp = await api.POST('/api/v1/parameter-schemas/{schema_id}/sets', {
      params: { path: { schema_id: schema.id } },
      body: { name: name.trim(), description: null, values: selectedNodeOverrides.value },
    })
    if (resp.error) {
      console.warn('Failed to create param set:', formatApiError(resp.error))
      return
    }
    await loadParamSets()
  } catch (err: any) {
    console.warn('Failed to create param set:', err)
  }
}

async function loadParamSets() {
  const schema = agentParamSchema(selectedNodeData.value?.agent_id)
  if (!schema) return
  try {
    const resp = await api.GET('/api/v1/parameter-schemas/{schema_id}/sets', {
      params: { path: { schema_id: schema.id } },
    })
    if (resp.data) paramSets.value = (resp.data as any) ?? []
  } catch (e) {
    console.warn('Failed to load param sets:', e)
  }
}

const canConvert = computed(() => pickerAgentId.value !== '__all__' && pickerConnectorId.value !== '__all__')

function convertBackendNode(n: any): any {
  const nodeType = n.node_type === 'manual' ? 'manual' : 'agent'
  return {
    id: n.id,
    type: nodeType,
    position: n.position || { x: 0, y: 0 },
    data: {
      label: n.label || 'Node ' + shortId(n.id),
      description: n.description || '',
      parameter_set_id: n.parameter_set_id,
      parameter_overrides: n.parameter_overrides,
    },
  }
}

function convertBackendEdge(e: any, i: number): any {
  const isLoop = e.edge_type === 'loop'
  const isLlm = e.edge_type === 'llm'
  let style: Record<string, string>
  if (isLoop) {
    style = { stroke: '#3b82f6', strokeDasharray: '5,5' }
  } else if (isLlm) {
    style = { stroke: '#8b5cf6' }
  } else {
    style = { stroke: '#888' }
  }
  return {
    id: e.id || `edge-${i}`,
    source: e.source_node_id,
    target: e.target_node_id,
    type: 'smoothstep',
    animated: isLoop,
    style,
    data: {
      hitl_gate_config: e.hitl_gate_config || null,
      edge_type: e.edge_type || 'normal',
      condition_expression: e.condition_expression || null,
      max_iterations: e.max_iterations || 0,
      routing_label: e.routing_label || '',
    },
  }
}

async function loadGraph() {
  pageError.value = null
  try {
    const { data, error: graphError } = await withTimeout((signal) => api.GET('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: pipelineId } },
      signal,
    }))
    if (graphError) {
      pageError.value = `Failed to load graph: ${formatApiError(graphError)}`
      return
    }
    const result = data as any
    if (!result) {
      rawNodes.value = []
      rawEdges.value = []
      flowNodes.value = []
      flowEdges.value = []
      return
    }
    rawNodes.value = result.nodes || []
    rawEdges.value = result.edges || []
    flowNodes.value = rawNodes.value.map(convertBackendNode)
    flowEdges.value = rawEdges.value.map(convertBackendEdge)
  } catch (e: unknown) {
    pageError.value = `Failed to load graph: ${formatApiError(e)}`
  }
}

async function loadCatalog() {
  pageError.value = null
  try {
    const [a, c, mb, s, snaps, ps] = await Promise.all([
      withTimeout((signal) => api.GET('/api/v1/agents', { signal }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
      withTimeout((signal) => api.GET('/api/v1/connectors', { signal }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
      withTimeout((signal) => api.GET('/api/v1/model-backends', { signal }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
      withTimeout((signal) => api.GET('/api/v1/schemas', { signal }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
      withTimeout((signal) => api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
        params: { path: { pipeline_id: pipelineId } },
        signal,
      }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
      withTimeout((signal) => api.GET('/api/v1/parameter-schemas', { signal }).then(r => (r.data as any)?.items ?? [])).catch(() => [] as any[]),
    ])
    agents.value = a
    connectors.value = c
    modelBackends.value = mb
    schemas.value = s
    snapshots.value = (snaps as any[]).filter((sn: any) => sn.snapshot_version > 0)
    paramSchemas.value = ps
  } catch (e) {
    console.warn('Failed to load pipeline data', e)
  }
}

function onNodeClick(event: any) {
  selectedEdgeData.value = null
  const node = event.node
  if (!node) return
  const backendNode = rawNodes.value.find((n: any) => n.id === node.id)
  selectedNodeData.value = backendNode || null
  // Populate parameter set + overrides
  if (backendNode?.parameter_set_id) {
    selectedNodeParamSetId.value = backendNode.parameter_set_id
    selectedNodeOverrides.value = { ...(backendNode.parameter_overrides ?? {}) }
    loadParamSets()
  } else {
    selectedNodeParamSetId.value = undefined
    selectedNodeOverrides.value = {}
  }
}

function onEdgeClick(event: any) {
  selectedNodeData.value = null
  const edge = event.edge
  if (!edge) return
  const backendEdge = rawEdges.value.find((e: any) => e.id === edge.id)
  if (backendEdge) {
    selectedEdgeData.value = backendEdge
    populateEdgeForm(backendEdge)
  }
}

function populateEdgeForm(edge: any) {
  edgeForm.edge_type = edge.edge_type || 'normal'
  edgeForm.condition_expression = edge.condition_expression || ''
  edgeForm.max_iterations = edge.max_iterations || 0
  edgeForm.routing_label = edge.routing_label || ''
  const hc = edge.hitl_gate_config
  if (hc) {
    edgeForm.hitl_enabled = true
    edgeForm.label = hc.label || ''
    edgeForm.description = hc.description || ''
    edgeForm.claim_expiry_minutes = hc.claim_expiry_minutes || 15
    edgeForm.human_only = hc.human_only || false
    if (hc.condition) {
      edgeForm.condition_type = 'jmespath'
      edgeForm.condition = hc.condition
      edgeForm.eval_name = ''
      edgeForm.eval_threshold = 0.8
      edgeForm.eval_operator = 'lt'
    } else if (hc.eval_condition) {
      edgeForm.condition_type = 'eval'
      edgeForm.eval_name = hc.eval_condition.eval_name || ''
      edgeForm.eval_threshold = hc.eval_condition.threshold ?? 0.8
      edgeForm.eval_operator = hc.eval_condition.operator || 'lt'
      edgeForm.condition = ''
    } else {
      edgeForm.condition_type = 'none'
      edgeForm.condition = ''
      edgeForm.eval_name = ''
      edgeForm.eval_threshold = 0.8
      edgeForm.eval_operator = 'lt'
    }
  } else {
    Object.assign(edgeForm, { ...defaultEdgeForm })
  }
}

function buildHitlGateConfig(): any {
  if (!edgeForm.hitl_enabled) return null
  const config: any = {
    label: edgeForm.label || 'Review Gate',
    description: edgeForm.description || '',
    reject_target: selectedEdgeData.value?.hitl_gate_config?.reject_target || null,
    claim_expiry_minutes: edgeForm.claim_expiry_minutes || 15,
    human_only: edgeForm.human_only || false,
    required_team_id: selectedEdgeData.value?.hitl_gate_config?.required_team_id || null,
  }
  if (edgeForm.condition_type === 'jmespath' && edgeForm.condition) {
    config.condition = edgeForm.condition
  }
  if (edgeForm.condition_type === 'eval' && edgeForm.eval_name) {
    config.eval_condition = {
      eval_name: edgeForm.eval_name,
      threshold: edgeForm.eval_threshold,
      operator: edgeForm.eval_operator,
    }
  }
  return config
}

async function saveEdgeConfig() {
  if (!selectedEdgeData.value) return
  savingEdge.value = true
  edgeSaveError.value = null

  const updatedEdges = rawEdges.value.map((e: any) => {
    if (e.id === selectedEdgeData.value.id) {
      return {
        id: e.id,
        source_node_id: e.source_node_id,
        target_node_id: e.target_node_id,
        edge_type: edgeForm.edge_type,
        condition_expression: edgeForm.condition_expression || null,
        max_iterations: edgeForm.edge_type === 'loop' ? (edgeForm.max_iterations || 0) : undefined,
        routing_label: edgeForm.edge_type === 'llm' ? (edgeForm.routing_label || undefined) : undefined,
        hitl_gate_config: buildHitlGateConfig(),
      }
    }
    return {
      id: e.id,
      source_node_id: e.source_node_id,
      target_node_id: e.target_node_id,
      edge_type: e.edge_type || 'normal',
      condition_expression: e.condition_expression || null,
      hitl_gate_config: e.hitl_gate_config || null,
    }
  })

  try {
    await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: pipelineId } },
      body: {
        nodes: rawNodes.value.map((n: any) => ({
          id: n.id,
          node_type: n.node_type || 'agent',
          mode: n.mode || 'llm',
          label: n.label || null,
          description: n.description || null,
          agent_id: n.agent_id || null,
          connector_binding: n.connector_binding || null,
          output_schema_id: n.output_schema_id || null,
          model_backend_id: n.model_backend_id || null,
          role: n.role || null,
          idempotent: n.idempotent !== false,
          timeout_seconds: n.timeout_seconds || null,
          stall_timeout_seconds: n.stall_timeout_seconds || null,
          enable_heartbeat: n.enable_heartbeat === false ? false : true,
          watch_log_path: n.watch_log_path || null,
          stdout_percentage_delta: n.stdout_percentage_delta ?? null,
          watch_globs: Array.isArray(n.watch_globs) ? n.watch_globs : [],
          position: n.position || null,
          read_only: n.read_only === true,
          git_credentials: n.git_credentials ?? null,
          parameter_set_id: n.parameter_set_id || null,
          parameter_overrides: n.parameter_overrides || null,
        })),
        edges: updatedEdges,
      },
      signal,
    }))
    await loadGraph()
    const updatedEdge = rawEdges.value.find((e: any) => e.id === selectedEdgeData.value.id)
    if (updatedEdge) {
      selectedEdgeData.value = updatedEdge
      populateEdgeForm(updatedEdge)
    }
  } catch (e: unknown) {
    edgeSaveError.value = formatApiError(e)
  } finally {
    savingEdge.value = false
  }
}

function onPaneClick() {
  selectedNodeData.value = null
  selectedEdgeData.value = null
  showSaveAsDropdown.value = false
  selectedNodeParamSetId.value = undefined
  selectedNodeOverrides.value = {}
}

function addNode() {
  const id = `node-${Date.now()}`
  const newNode = {
    id,
    type: 'agent',
    position: { x: 250, y: 100 },
    data: { label: 'New Node', description: '' },
  }
  flowNodes.value = [...flowNodes.value, newNode]
  rawNodes.value = [...rawNodes.value, {
    id,
    node_type: 'agent',
    label: 'New Node',
    description: '',
    position: { x: 250, y: 100 },
  }]
}

function openAgentPicker() {
  convertError.value = null
  pickerAgentId.value = '__all__'
  pickerConnectorId.value = '__all__'
  showAgentPicker.value = true
}

function openRevertDialog() {
  revertError.value = null
  revertSnapshotId.value = '__all__'
  showRevertDialog.value = true
}

function openSaveAsComposite() {
  showSaveAsDropdown.value = false
  saveAsName.value = ''
  saveAsDescription.value = ''
  saveAsSelectedNodeIds.value = rawNodes.value.map((n: any) => n.id)
  saveAsError.value = null
  showSaveAsComposite.value = true
}

async function handleSaveAsComposite() {
  if (!saveAsName.value || saveAsSelectedNodeIds.value.length === 0) return
  saving.value = true
  saveAsError.value = null
  try {
    await withTimeout((signal) => api.POST('/api/v1/pipelines/{pipeline_id}/save-as-composite', {
      params: { path: { pipeline_id: pipelineId } },
      body: {
        name: saveAsName.value,
        description: saveAsDescription.value || null,
        selected_node_ids: saveAsSelectedNodeIds.value,
      },
      signal,
    }))
    showSaveAsComposite.value = false
    router.push({ name: 'library' })
  } catch (e: unknown) {
    saveAsError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

function onAgentChange() {
  pickerConnectorId.value = '__all__'
}

async function convertToAgent() {
  if (!canConvert.value || !selectedNodeData.value) return
  convertError.value = null
  try {
    const nodeId = selectedNodeData.value.id
    await withTimeout((signal) => api.POST('/api/v1/pipelines/{pipeline_id}/nodes/{node_id}/convert-to-agent', {
      params: { path: { pipeline_id: pipelineId, node_id: nodeId } },
      body: {
        agent_id: pickerAgentId.value,
        connector_binding: {
          type: connectors.value.find(c => c.id === pickerConnectorId.value)?.connector_type_id || '',
          instance_id: pickerConnectorId.value,
        },
        model_backend_id: selectedAgent.value?.model_backend_id,
      },
      signal,
    }))
    showAgentPicker.value = false
    await loadGraph()
    selectedNodeData.value = rawNodes.value.find((n: any) => n.id === nodeId) || null
  } catch (e: unknown) {
    convertError.value = formatApiError(e)
  }
}

async function revertToManual() {
  if (revertSnapshotId.value === '__all__' || !selectedNodeData.value) return
  revertError.value = null
  revertLoading.value = true
  try {
    const nodeId = selectedNodeData.value.id
    await withTimeout((signal) => api.POST('/api/v1/pipelines/{pipeline_id}/nodes/{node_id}/revert-to-manual', {
      params: {
        path: { pipeline_id: pipelineId, node_id: nodeId },
        query: { snapshot_id: revertSnapshotId.value },
      },
      signal,
    }))
    showRevertDialog.value = false
    await loadGraph()
    selectedNodeData.value = rawNodes.value.find((n: any) => n.id === nodeId) || null
  } catch (e: unknown) {
    revertError.value = formatApiError(e)
  } finally {
    revertLoading.value = false
  }
}

async function loadPipeline() {
  pageError.value = null
  try {
    const { data } = await withTimeout((signal) => api.GET('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      signal,
    }))
    pipeline.value = data as any
    maxDurationInput.value = (data as any)?.max_duration_seconds ?? undefined
    syncRetryPolicyFromPipeline()
  } catch (e) {
    pageError.value = `Failed to load pipeline: ${formatApiError(e)}`
  }
}

function openRenameDialog() {
  renameName.value = pipeline.value?.name || ''
  renameError.value = null
  showRenameDialog.value = true
}

async function handleRename() {
  if (!renameName.value.trim()) return
  renaming.value = true
  renameError.value = null
  try {
    const { data } = await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      body: { name: renameName.value.trim() },
      signal,
    }))
    pipeline.value = data as any
    showRenameDialog.value = false
  } catch (e: unknown) {
    renameError.value = formatApiError(e)
  } finally {
    renaming.value = false
  }
}

async function handleArchive() {
  try {
    pipeline.value = await postUntyped<Record<string, unknown>>(`/api/v1/pipelines/${pipelineId}/archive`)
  } catch (e: unknown) {
    pageError.value = `Failed to archive pipeline: ${formatApiError(e)}`
  }
}

async function handleUnarchive() {
  try {
    pipeline.value = await postUntyped<Record<string, unknown>>(`/api/v1/pipelines/${pipelineId}/unarchive`)
  } catch (e: unknown) {
    pageError.value = `Failed to unarchive pipeline: ${formatApiError(e)}`
  }
}

async function handleDelete() {
  deleteError.value = null
  try {
    await withTimeout((signal) => api.DELETE('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      signal,
    }))
    router.push({ name: 'library' })
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  }
}

async function updateMaxDuration() {
  const val = maxDurationInput.value && maxDurationInput.value > 0 ? maxDurationInput.value : undefined
  try {
    await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: pipelineId } },
      body: { max_duration_seconds: val },
      signal,
    }))
    if (pipeline.value) pipeline.value.max_duration_seconds = val
    saveGraphError.value = null
  } catch (e) {
    saveGraphError.value = `Failed to update max duration: ${formatApiError(e)}`
  }
}

function openRunDialog() {
  runPrompt.value = ''
  runError.value = null
  confirmEmptyRun.value = false
  emptyRunWarning.value = null
  showRunDialog.value = true
}

function closeRunDialog() {
  showRunDialog.value = false
  runPrompt.value = ''
  runError.value = null
  confirmEmptyRun.value = false
  emptyRunWarning.value = null
}

async function saveGraph() {
  savingGraph.value = true
  saveGraphError.value = null
  try {
    // Sync current param set + overrides into selected node data
    if (selectedNodeData.value) {
      selectedNodeData.value.parameter_set_id = selectedNodeParamSetId.value || null
      selectedNodeData.value.parameter_overrides = Object.keys(selectedNodeOverrides.value).length > 0
        ? { ...selectedNodeOverrides.value }
        : null
    }
    await withTimeout((signal) => api.PATCH('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: pipelineId } },
      body: {
        nodes: rawNodes.value.map((n: any) => ({
          id: n.id,
          node_type: n.node_type || 'agent',
          mode: n.mode || 'llm',
          label: n.label || null,
          description: n.description || null,
          agent_id: n.agent_id || null,
          connector_binding: n.connector_binding || null,
          output_schema_id: n.output_schema_id || null,
          model_backend_id: n.model_backend_id || null,
          role: n.role || null,
          idempotent: n.idempotent !== false,
          timeout_seconds: n.timeout_seconds || null,
          stall_timeout_seconds: n.stall_timeout_seconds || null,
          enable_heartbeat: n.enable_heartbeat === false ? false : true,
          watch_log_path: n.watch_log_path || null,
          stdout_percentage_delta: n.stdout_percentage_delta ?? null,
          watch_globs: Array.isArray(n.watch_globs) ? n.watch_globs : [],
          position: n.position || null,
          read_only: n.read_only === true,
          git_credentials: n.git_credentials ?? null,
          parameter_set_id: n.parameter_set_id || null,
          parameter_overrides: n.parameter_overrides || null,
        })),
        edges: rawEdges.value.map((e: any) => ({
          id: e.id,
          source_node_id: e.source_node_id,
          target_node_id: e.target_node_id,
          edge_type: e.edge_type || 'normal',
          condition_expression: e.condition_expression || null,
          max_iterations: e.edge_type === 'loop' ? (e.max_iterations || 0) : undefined,
          routing_label: e.edge_type === 'llm' ? (e.routing_label || undefined) : undefined,
          hitl_gate_config: e.hitl_gate_config || null,
        })),
      },
      signal,
    }))
    // Reload graph to sync flowNodes with saved state
    await loadGraph()
  } catch (e: unknown) {
    saveGraphError.value = formatApiError(e)
  } finally {
    savingGraph.value = false
  }
}

async function triggerRun() {
  if (!pipeline.value) return
  const trimmedPrompt = runPrompt.value.trim()
  if (!trimmedPrompt) {
    if (!confirmEmptyRun.value) {
      confirmEmptyRun.value = true
      emptyRunWarning.value = 'No input provided \u2014 this pipeline will run with an empty input payload. Are you sure?'
      return
    }
    emptyRunWarning.value = null
  }
  running.value = true
  runError.value = null
  try {
    await saveGraph()
    if (saveGraphError.value) {
      runError.value = `Failed to save graph: ${saveGraphError.value}`
      return
    }
    const { data } = await withTimeout((signal) => api.POST('/api/v1/runs', {
      body: {
        pipeline_id: pipelineId,
        input_payload: trimmedPrompt ? { prompt: trimmedPrompt } : {},
      },
      signal,
    }))
    showRunDialog.value = false
    if (data) router.push({ name: 'run-detail', params: { id: (data as any).id } })
  } catch (e: unknown) {
    runError.value = formatApiError(e)
  } finally {
    running.value = false
  }
}

async function loadFolders() {
  try {
    folders.value = await get<any[]>('/api/v1/pipeline-folders')
  } catch (e) {
    console.warn('Failed to load folders', e)
  }
}

async function loadLifecycleMaps() {
  pageError.value = null
  try {
    const response = await get<any[] | { items?: any[] }>('/api/v1/lifecycle-maps')
    const summaries = Array.isArray(response) ? response : (response.items ?? [])
    const first10 = (summaries ?? []).slice(0, 10)
    const fullMaps = await Promise.all(
      first10.map((m: any) =>
        get<any>(`/api/v1/lifecycle-maps/${m.id}`).catch(() => null)
      )
    )
    linkedLifecycleMaps.value = fullMaps.filter(
      (m: any) => m && m.stages?.some((s: any) => s.pipeline_id === pipelineId)
    )
  } catch (e) {
    console.warn('Failed to load lifecycle maps', e)
  }
}

const { loading, error: pageErrorRef } = useDataFetch(
  async () => {
    pageErrorRef.value = null
    await Promise.all([loadPipeline(), loadGraph(), loadCatalog(), loadFolders(), loadLifecycleMaps()])
    return { data: {} }
  },
  { initialValue: {} },
)

const pageError = pageErrorRef as any as ReturnType<typeof ref<string | null>>
</script>
