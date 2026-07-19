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
            <Button variant="default" size="xs" @click="openRenameDialog">Rename</Button>
            <button v-if="!pipeline?.archived_at" class="rounded-md border border-input bg-background px-3 py-1 text-xs font-medium hover:bg-accent" @click="handleArchive">Archive</button>
            <button v-else class="rounded-md border border-input bg-background px-3 py-1 text-xs font-medium hover:bg-accent" @click="handleUnarchive">Unarchive</button>
            <button v-if="planStore.featureEnabled('pipeline_delete')" class="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-1 text-xs font-medium text-destructive hover:bg-destructive/20" @click="showDeleteConfirm = true">Delete</button>
            <Button variant="outline" size="xs" @click="addNode">Add Node</Button>
          </div>
        </div>
        <!-- Toolbar -->
        <div class="absolute left-4 top-4 z-10 flex items-center gap-2 rounded-lg border bg-card px-3 py-2 shadow-sm">
          <div class="flex items-center gap-2">
            <h2 class="text-sm font-semibold">{{ pipeline?.name || $t('views.PipelineEditorView.pipeline_editor') }}</h2>
            <button class="rounded p-1 hover:bg-accent" @click="openRenameDialog" title="Rename pipeline">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
            </button>
            <span v-if="pipeline?.archived_at" class="rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning">Archived</span>
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
          <Button
            variant="default"
            size="xs"
            :disabled="savingGraph"
            @click="saveGraph"
            data-testid="pipeline-editor-save"
          >
            <svg v-if="savingGraph" class="mr-1 h-3 w-3 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
            {{ savingGraph ? $t('views.PipelineEditorView.saving_graph') : $t('views.PipelineEditorView.save') }}
          </Button>
          <span v-if="saveGraphError" class="ml-2 text-xs text-destructive" data-testid="pipeline-editor-save-error">{{ saveGraphError }}</span>
          <Button
            variant="default"
            size="xs"
            class="border-indigo-300 bg-indigo-600 text-white hover:bg-indigo-500"
            :disabled="running || flowNodes.length === 0"
            :title="flowNodes.length === 0 ? $t('views.PipelineEditorView.no_nodes_to_run') : ''"
            @click="openRunDialog"
            data-testid="pipeline-editor-run"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="currentColor" class="mr-1"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            {{ running ? $t('views.PipelineEditorView.running') : $t('views.PipelineEditorView.run_pipeline') }}
          </Button>
          <div class="relative" @click.stop>
            <button
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
            <button v-if="!pipeline?.archived_at" class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent" @click="handleArchive">Archive</button>
            <button v-else class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent" @click="handleUnarchive">Unarchive</button>
            <button v-if="planStore.featureEnabled('pipeline_delete')" class="rounded-md border border-destructive/50 bg-destructive/10 px-2 py-1 text-xs font-medium text-destructive hover:bg-destructive/20" @click="showDeleteConfirm = true">Delete</button>
          </div>
          <span class="mx-2 h-4 w-px bg-border" />
          <div class="flex items-center gap-1">
            <label for="pipeline-max-duration" class="text-[10px] text-muted-foreground whitespace-nowrap">Max Duration (s):</label>
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
          <button
            class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent flex items-center gap-1"
            @click="addNode"
            title="Add node"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            Add Node
          </button>
          <span class="mx-2 h-4 w-px bg-border" />
          <button
            class="rounded-md border border-input bg-background px-2 py-1 text-xs font-medium hover:bg-accent flex items-center gap-1"
            @click="() => fitView()"
            title="Fit view"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
            Fit View
          </button>
        </div>

        <!-- Run dialog modal -->
        <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
          v-if="showRunDialog"
          class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
          @click.self="closeRunDialog"
        >
          <div class="bg-card border border-border rounded-xl shadow-xl w-full max-w-lg mx-4 p-6 space-y-4">
            <div class="flex items-center justify-between">
              <h2 class="text-base font-semibold text-foreground">{{ $t('views.PipelineEditorView.run_dialog_title') }}</h2>
              <button
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
              <label for="pipeline-editor-run-prompt" class="block text-sm font-medium text-foreground">Prompt</label>
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

            <div class="flex justify-end gap-2 pt-2">
              <button
                class="px-4 py-2 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
                @click="closeRunDialog"
              >
                {{ $t('views.PipelineEditorView.cancel') }}
              </button>
              <Button
                v-if="!isWebhookTriggered"
                variant="default"
                class="border-indigo-300 bg-indigo-600 text-white hover:bg-indigo-500"
                :disabled="running"
                @click="triggerRun"
                data-testid="pipeline-editor-run-submit"
              >
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
          </div>
        </div>

        <VueFlow
          v-model:nodes="flowNodes"
          v-model:edges="flowEdges"
          :node-types="nodeTypes"
          :default-edge-options="{ type: 'smoothstep', animated: false, style: { stroke: '#888' } }"
          fit-view-on-init
          @node-click="onNodeClick"
          @edge-click="onEdgeClick"
          @pane-click="onPaneClick"
        >
          <Background :gap="20" :size="1" />
          <Controls :showInteractive="false" />
          <template #node-manual="nodeProps">
            <div class="rounded-lg border-2 border-warning/60 bg-warning/10 px-4 py-2 shadow-sm">
              <div class="text-xs font-medium text-warning">MANUAL</div>
              <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
            </div>
          </template>
          <template #node-agent="nodeProps">
            <div class="rounded-lg border-2 border-primary/60 bg-primary/10 px-4 py-2 shadow-sm">
              <div class="text-xs font-medium text-primary">AGENT</div>
              <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
            </div>
          </template>
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
            <dd class="font-mono text-[10px] text-muted-foreground break-all select-all">{{ selectedNodeData.id }}</dd>
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
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">{{ $t('views.PipelineEditorView.label_field') }}</dt>
            <dd>{{ selectedNodeData.label || '-' }}</dd>
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
                    v-model.number="selectedNodeOverrides[pkey]"
                    type="number"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  />
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'boolean'"
                    v-model="selectedNodeOverrides[pkey]"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option :value="true">true</option>
                    <option :value="false">false</option>
                  </select>
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'select'"
                    v-model="selectedNodeOverrides[pkey]"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option v-for="o in (paramDefByKey(pkey)?.options || [])" :key="o" :value="o">{{ o }}</option>
                  </select>
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'model_backend_ref'"
                    v-model="selectedNodeOverrides[pkey]"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option v-for="mb in modelBackends" :key="mb.id" :value="mb.id">{{ mb.display_name || mb.name || mb.id }}</option>
                  </select>
                  <select
                    v-else-if="paramDefByKey(pkey)?.type === 'schema_ref'"
                    v-model="selectedNodeOverrides[pkey]"
                    class="w-full rounded-lg border border-input bg-background px-2 py-1 text-xs"
                  >
                    <option :value="undefined"></option>
                    <option v-for="s in schemas" :key="s.id" :value="s.id">{{ s.name || s.id }}</option>
                  </select>
                  <span v-else class="text-xs text-muted-foreground">â€”</span>
                </div>
                <button
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
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">Template</dt>
              <dd class="font-mono text-xs">{{ selectedNodeData.template_id }}</dd>
            </div>
            <div v-if="selectedNodeData.agent_command">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">Command</dt>
              <dd class="font-mono text-xs break-all">{{ selectedNodeData.agent_command }}</dd>
            </div>
            <div v-if="selectedNodeData.timeout_seconds">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">Timeout</dt>
              <dd>{{ selectedNodeData.timeout_seconds }}s</dd>
            </div>
            <div v-if="selectedNodeData.env_vars && Object.keys(selectedNodeData.env_vars).length > 0">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">Env Vars</dt>
              <dd class="font-mono text-[10px] break-all">{{ Object.keys(selectedNodeData.env_vars).join(', ') }}</dd>
            </div>
            <div v-if="selectedNodeData.context_files && Object.keys(selectedNodeData.context_files).length > 0">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">Context Files</dt>
              <dd><ul class="list-inside list-disc text-xs text-muted-foreground"><li v-for="(content, fpath) in selectedNodeData.context_files" :key="fpath">{{ fpath }} <span class="text-[10px] opacity-60">({{ content.length }} bytes)</span></li></ul></dd>
            </div>
            <div v-if="selectedNodeData.agent_prompt">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">Prompt</dt>
              <dd class="text-xs text-muted-foreground italic whitespace-pre-wrap max-h-32 overflow-y-auto">{{ selectedNodeData.agent_prompt.substring(0, 300) }}{{ selectedNodeData.agent_prompt.length > 300 ? '...' : '' }}</dd>
            </div>
          </template>

          <template v-if="selectedNodeData.node_type === 'sandbox_agent'">
            <div v-if="selectedNodeData.template_id">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">Template</dt>
              <dd class="font-mono text-xs">{{ selectedNodeData.template_id }}</dd>
            </div>
            <div v-if="selectedNodeData.agent_command">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">Command</dt>
              <dd class="font-mono text-xs break-all">{{ selectedNodeData.agent_command }}</dd>
            </div>
            <div v-if="selectedNodeData.timeout_seconds">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">Timeout</dt>
              <dd>{{ selectedNodeData.timeout_seconds }}s</dd>
            </div>
            <div v-if="selectedNodeData.agent_prompt">
              <dt class="text-muted-foreground text-xs uppercase tracking-wider">Prompt</dt>
              <dd class="text-xs text-muted-foreground italic">{{ selectedNodeData.agent_prompt.substring(0, 200) }}{{ selectedNodeData.agent_prompt.length > 200 ? '...' : '' }}</dd>
            </div>
          </template>

          <!-- Lifecycle maps -->
          <div v-if="linkedLifecycleMaps.length > 0">
            <dt class="text-muted-foreground text-xs uppercase tracking-wider">Lifecycle Maps</dt>
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
          <Button
            v-if="selectedNodeData.node_type === 'manual'"
            variant="default"
            class="w-full"
            data-testid="pipeline-editor-convert-to-agent"
            @click="openAgentPicker"
          >
            Convert to Agent
          </Button>
          <button
            v-if="selectedNodeData.node_type === 'agent'"
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
        <h2 class="mb-4 text-base font-semibold">Edge Properties</h2>
        <dl class="space-y-3 text-sm">
          <div>
            <dt class="text-muted-foreground">Source</dt>
            <dd class="font-mono text-xs">{{ shortId(selectedEdgeData.source_node_id) }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Target</dt>
            <dd class="font-mono text-xs">{{ shortId(selectedEdgeData.target_node_id) }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">{{ $t('views.PipelineEditorView.type_label') }}</dt>
            <dd>
              <Select v-model="edgeForm.edge_type">
                <SelectTrigger aria-label="Edge type" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm">
                  <SelectValue placeholder="Normal" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="normal">Normal</SelectItem>
                  <SelectItem value="reject">Reject</SelectItem>
                  <SelectItem value="conditional">Conditional</SelectItem>
                  <SelectItem value="loop">Loop</SelectItem>
                  <SelectItem value="llm">LLM Routing</SelectItem>
                </SelectContent>
              </Select>
            </dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Condition Expression</dt>
            <dd>
              <input
                v-model="edgeForm.condition_expression"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                placeholder="JMESPath expression (e.g. score > `0.5`)"
                :disabled="edgeForm.edge_type !== 'conditional' && edgeForm.edge_type !== 'loop'"
              />
            </dd>
          </div>
          <div v-if="edgeForm.edge_type === 'loop'">
            <dt class="text-muted-foreground">Max Iterations</dt>
            <dd>
              <input
                v-model.number="edgeForm.max_iterations"
                type="number"
                min="0"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="0 = unlimited (RunawayGuard applies)"
              />
              <p class="mt-1 text-xs text-muted-foreground">Maximum number of times this loop can repeat before exiting. 0 means no limit.</p>
            </dd>
          </div>
          <div v-if="edgeForm.edge_type === 'llm'">
            <dt class="text-muted-foreground">Routing Label</dt>
            <dd>
              <input
                v-model="edgeForm.routing_label"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                placeholder="e.g. retry, escalate, complete"
              />
              <p class="mt-1 text-xs text-muted-foreground">The LLM uses this label to select this path. Must be unique among outgoing edges.</p>
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
            <span class="ml-2 text-xs text-muted-foreground">Enabled</span>
          </label>
        </div>

        <div v-if="edgeForm.hitl_enabled" class="mt-4 space-y-4">
          <div>
            <label for="pipelineeditorview-field-14" class="mb-1 block text-xs font-medium text-muted-foreground">Label</label>
            <input id="pipelineeditorview-field-14"
              v-model="edgeForm.label"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="e.g. Review before deploy"
            />
          </div>
          <div>
            <label for="pipelineeditorview-field-13" class="mb-1 block text-xs font-medium text-muted-foreground">Description</label>
            <textarea id="pipelineeditorview-field-13"
              v-model="edgeForm.description"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="Describe what the reviewer should check"
              rows="2"
            />
          </div>
          <div>
            <label for="pipelineeditorview-field-12" class="mb-1 block text-xs font-medium text-muted-foreground">Claim Expiry (minutes)</label>
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
            <span class="text-xs text-muted-foreground">Human only (block LLM auto-approval)</span>
          </div>

          <hr class="border-t" />

          <div>
            <label for="pipelineeditorview-field-11" class="mb-1 block text-xs font-medium text-muted-foreground">Condition Type</label>
            <Select v-model="edgeForm.condition_type">
              <SelectTrigger aria-label="Condition type" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm">
                <SelectValue placeholder="None (always gate)" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None (always gate)</SelectItem>
                <SelectItem value="jmespath">JMESPath Expression</SelectItem>
                <SelectItem value="eval">Eval Reference</SelectItem>
              </SelectContent>
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
              <label for="pipelineeditorview-field-9" class="mb-1 block text-xs font-medium text-muted-foreground">Eval Name</label>
              <input id="pipelineeditorview-field-9"
                v-model="edgeForm.eval_name"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                placeholder="e.g. quality-check"
              />
            </div>
            <div class="flex gap-2">
              <div class="flex-1">
                <label for="pipelineeditorview-field-8" class="mb-1 block text-xs font-medium text-muted-foreground">Threshold</label>
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
                <label for="pipelineeditorview-field-7" class="mb-1 block text-xs font-medium text-muted-foreground">Operator</label>
                <Select v-model="edgeForm.eval_operator">
                  <SelectTrigger aria-label="Operator" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm">
                    <SelectValue placeholder="lt (score &lt; threshold)" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="lt">lt (score &lt; threshold)</SelectItem>
                    <SelectItem value="gt">gt (score &gt; threshold)</SelectItem>
                    <SelectItem value="lte">lte (score &le; threshold)</SelectItem>
                    <SelectItem value="gte">gte (score &ge; threshold)</SelectItem>
                    <SelectItem value="eq">eq (score == threshold)</SelectItem>
                    <SelectItem value="neq">neq (score != threshold)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <p class="mt-1 text-[10px] text-muted-foreground">
              If condition is true, gate fires. If false, gate is skipped.
            </p>
          </div>

          <div class="flex gap-2 pt-2">
            <Button
              data-testid="pipeline-editor-save-edge"
              class="flex-1"
              :disabled="savingEdge"
              @click="saveEdgeConfig"
            >
              {{ savingEdge ? 'Saving...' : 'Save Edge' }}
            </Button>
            <button
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
            <Select v-model="pickerAgentId" @update:model-value="onAgentChange">
              <SelectTrigger data-testid="pipeline-editor-agent-select" aria-label="Agent" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm">
                <SelectValue :placeholder="$t('views.PipelineEditorView.select_agent_placeholder')" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">{{ $t('views.PipelineEditorView.select_agent_placeholder') }}</SelectItem>
                <SelectItem v-for="a in agents" :key="a.id" :value="a.id">{{ a.name }}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div v-if="selectedAgent">
            <label for="pipelineeditorview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.connector') }}</label>
            <Select v-model="pickerConnectorId">
              <SelectTrigger data-testid="pipeline-editor-connector-select" aria-label="Connector" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm">
                <SelectValue :placeholder="$t('views.PipelineEditorView.select_connector_placeholder')" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">{{ $t('views.PipelineEditorView.select_connector_placeholder') }}</SelectItem>
                <SelectItem v-for="c in eligibleConnectors" :key="c.id" :value="c.id">{{ c.name }} ({{ c.connector_type_id }})</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div v-if="selectedAgent">
            <span class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.model_backend_label') }}</span>
            <div class="rounded-lg border bg-muted px-3 py-2 text-sm">
              {{ modelBackendName || $t('views.PipelineEditorView.loading') }}
            </div>
          </div>
          <div v-if="selectedAgent" class="rounded-lg border bg-muted p-3 text-sm">
            <p class="text-xs text-muted-foreground">Schema</p>
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
          <Select v-model="revertSnapshotId">
            <SelectTrigger data-testid="pipeline-editor-snapshot-select" aria-label="Snapshot" class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm">
              <SelectValue :placeholder="$t('views.PipelineEditorView.select_snapshot_placeholder')" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__all__">{{ $t('views.PipelineEditorView.select_snapshot_placeholder') }}</SelectItem>
              <SelectItem
                v-for="s in snapshots"
                :key="s.id"
                :value="s.id"
              >
                v{{ s.snapshot_version }}{{ s.tag ? ` â€” ${s.tag}` : '' }}
              </SelectItem>
            </SelectContent>
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
          <label for="pipelineeditorview-field-2" class="mb-1 block text-sm font-medium">Description</label>
          <textarea id="pipelineeditorview-field-2"
            v-model="saveAsDescription"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            rows="3"
            placeholder="Optional description"
          />
        </div>
        <div>
          <span class="mb-1 block text-sm font-medium">Selected Nodes</span>
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
          <label for="pipelineeditorview-field-1" class="mb-1 block text-sm font-medium">Name</label>
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
import { ref, computed, reactive } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { VueFlow, useVueFlow } from '@vue-flow/core'
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
import { Button } from '@/components/ui/button'
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '@/components/ui/select'

function withTimeout<T>(factory: (signal: AbortSignal) => Promise<T>, ms = 15000): Promise<T> {
  const ctrl = new AbortController()
  const timeout = setTimeout(() => ctrl.abort(), ms)
  return factory(ctrl.signal).finally(() => clearTimeout(timeout))
}

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
const maxDurationInput = ref<number | undefined>(undefined)

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
  const allowedTypes = refs.map(r => r.connector_type)
  return connectors.value.filter(c => allowedTypes.includes(c.connector_type_id))
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
  selectedNodeOverrides.value = { ...(set?.values || {}) }
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
      parameter_set_id: n.parameter_set_id,
      parameter_overrides: n.parameter_overrides,
    },
  }
}

function convertBackendEdge(e: any, i: number): any {
  const isLoop = e.edge_type === 'loop'
  const isLlm = e.edge_type === 'llm'
  return {
    id: e.id || `edge-${i}`,
    source: e.source_node_id,
    target: e.target_node_id,
    type: 'smoothstep',
    animated: isLoop,
    style: isLoop
      ? { stroke: '#3b82f6', strokeDasharray: '5,5' }
      : isLlm
        ? { stroke: '#8b5cf6' }
        : { stroke: '#888' },
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
    selectedNodeOverrides.value = { ...(backendNode.parameter_overrides || {}) }
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
          label: n.label || null,
          agent_id: n.agent_id || null,
          connector_binding: n.connector_binding || null,
          output_schema_id: n.output_schema_id || null,
          model_backend_id: n.model_backend_id || null,
          role: n.role || null,
          timeout_seconds: n.timeout_seconds || null,
          position: n.position || null,
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
    data: { label: 'New Node' },
  }
  flowNodes.value = [...flowNodes.value, newNode]
  rawNodes.value = [...rawNodes.value, {
    id,
    node_type: 'agent',
    label: 'New Node',
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
  const val = maxDurationInput.value && maxDurationInput.value > 0 ? maxDurationInput.value : null
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
  showRunDialog.value = true
}

function closeRunDialog() {
  showRunDialog.value = false
  runPrompt.value = ''
  runError.value = null
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
          label: n.label || null,
          agent_id: n.agent_id || null,
          connector_binding: n.connector_binding || null,
          output_schema_id: n.output_schema_id || null,
          model_backend_id: n.model_backend_id || null,
          role: n.role || null,
          timeout_seconds: n.timeout_seconds || null,
          position: n.position || null,
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
  } catch (e: unknown) {
    saveGraphError.value = formatApiError(e)
  } finally {
    savingGraph.value = false
  }
}

async function triggerRun() {
  if (!pipeline.value) return
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
        prompt: runPrompt.value.trim() || undefined,
        payload: {},
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
