<template>
  <BackLink to="/library" label="Back to Library" />
  <div class="min-h-screen bg-background">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="max-w-3xl mx-auto">
        <button type="button"
          class="text-sm text-muted-foreground hover:text-foreground mb-2 inline-flex items-center gap-1"
          @click="router.push({ name: 'library' })"
          data-testid="library-wizard-back"
        >
          <ArrowLeft class="h-4 w-4" />
          {{ $t('views.LibraryPipelineWizard.back_to_library') }}
        </button>
        <PageHeader :title="$t('views.LibraryPipelineWizard.create_pipeline_from_template')" />
      </div>
    </header>

    <main class="max-w-3xl mx-auto px-6 py-8">
      <LoadingSpinner v-if="loading" />

      <ErrorAlert v-else-if="error" :message="error" class="mb-6" />

      <div v-else>
        <div class="card p-6 mb-6">
          <h2 class="text-lg font-medium text-foreground mb-1">{{ primitive?.name }}</h2>
          <p v-if="primitive?.description" class="text-sm text-muted-foreground mb-4">
            {{ primitive.description }}
          </p>

          <div class="flex items-center gap-2 mb-4">
            <span
              v-for="tag in (primitive?.tags || []).slice(0, 5)"
              :key="tag"
              class="badge badge-tag"
            >
              {{ tag }}
            </span>
          </div>

          <div class="bg-muted rounded-lg p-4 text-sm text-foreground">
            <p><strong>{{ $t('views.LibraryPipelineWizard.author') }}</strong> {{ primitive?.author }}</p>
            <p><strong>{{ $t('views.LibraryPipelineWizard.version') }}</strong> {{ primitive?.version }}</p>
          </div>
        </div>

        <!-- Sub-pipeline visualization for composites -->
        <div
          v-if="subGraphNodes.length > 0"
          class="card p-6 mb-6"
        >
          <h3 class="text-base font-medium text-foreground mb-4">{{ $t('views.LibraryPipelineWizard.pipeline_visualisation') }}</h3>
          <p class="text-sm text-muted-foreground mb-4">
            {{ $t('views.LibraryPipelineWizard.sub_pipeline_internal') }}
          </p>
          <div class="h-72 rounded-lg border bg-card overflow-hidden">
            <VueFlow
              :nodes="subGraphNodes"
              :edges="subGraphEdges"
              :node-types="subNodeTypes"
              :default-edge-options="{ type: 'smoothstep', animated: false, style: { stroke: '#888' } }"
              fit-view-on-init
              :nodes-draggable="false"
              :nodes-connectable="false"
              :elements-selectable="false"
            >
              <Background :gap="20" :size="1" />
              <template #node-agent="nodeProps">
                <div class="rounded-lg border-2 border-indigo-500/60 bg-indigo-500/10 px-4 py-3 shadow-sm">
                  <div class="text-xs font-medium text-indigo-400">{{ $t('views.LibraryPipelineWizard.agent') }}</div>
                  <div class="text-sm font-semibold text-foreground">{{ nodeProps.data.label }}</div>
                </div>
              </template>
            </VueFlow>
          </div>
        </div>

        <div class="card p-6 mb-6">
          <h3 class="text-base font-medium text-foreground mb-4">{{ $t('views.LibraryPipelineWizard.pipeline_configuration') }}</h3>

          <div class="space-y-4">
            <div>
              <label for="librarypipelinewizard-field-2" class="block text-sm font-medium text-foreground mb-1">{{ $t('views.LibraryPipelineWizard.pipeline_name') }}</label>
              <input id="librarypipelinewizard-field-2"
                v-model="pipelineName"
                type="text"
                class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                :placeholder="$t('views.LibraryPipelineWizard.primitivename_pipeline_from_template')"
                data-testid="library-wizard-pipeline-name"
              />
            </div>

            <div>
              <label for="librarypipelinewizard-field-1" class="block text-sm font-medium text-foreground mb-1">{{ $t('views.LibraryPipelineWizard.description') }}</label>
              <textarea id="librarypipelinewizard-field-1"
                v-model="pipelineDescription"
                rows="3"
                class="w-full px-3 py-2 border border-input bg-background rounded-lg text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                :placeholder="$t('views.LibraryPipelineWizard.primitivedescription_pipeline_created_from_library_template')"
                data-testid="library-wizard-description"
              />
            </div>
          </div>
        </div>

        <div class="card p-6 mb-6">
          <h3 class="text-base font-medium text-foreground mb-4">{{ $t('views.LibraryPipelineWizard.ownership') }}</h3>
          <p class="text-sm text-muted-foreground mb-4">{{ $t('views.LibraryPipelineWizard.choose_who_this_pipeline_belongs_to_orgwide_pipelines_are_vi') }}</p>
          <OwnershipPicker v-model="ownership" :label="$t('views.LibraryPipelineWizard.owner')" />
        </div>

        <div v-if="templateAgents.length > 0" class="card p-6 mb-6">
          <h3 class="text-base font-medium text-foreground mb-4">{{ $t('views.LibraryPipelineWizard.template_agents', { count: templateAgents.length }) }}</h3>
          <div class="space-y-3">
            <div
              v-for="(agent, i) in templateAgents"
              :key="i"
              class="flex items-start gap-3 p-3 bg-muted rounded-lg"
            >
              <div class="w-6 h-6 rounded-full badge badge-context-blue flex items-center justify-center shrink-0">
                {{ i + 1 }}
              </div>
              <div>
                <p class="text-sm font-medium text-foreground">{{ agent.name }}</p>
                <p v-if="agent.description" class="text-xs text-muted-foreground mt-0.5">{{ agent.description }}</p>
                <div class="flex gap-2 mt-1">
                  <span
                    v-for="ref in (agent.connector_type_refs || [])"
                    :key="ref.connector_type"
                    class="badge badge-context-purple"
                  >
                    {{ ref.connector_type }}
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="flex items-center gap-3">
          <Button :disabled="creating" class="px-6 py-2.5" @click="createPipeline" data-testid="library-wizard-create">
            {{ creating ? $t('views.LibraryPipelineWizard.creating') : $t('views.LibraryPipelineWizard.create_pipeline') }}
          </Button>
          <button type="button"
            class="px-6 py-2.5 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
            @click="router.push({ name: 'library' })"
            data-testid="library-wizard-cancel"
          >
            {{ $t('views.LibraryPipelineWizard.cancel') }}
          </button>
        </div>

        <div
          v-if="result"
          class="mt-6 rounded-lg border border-success/50 bg-success/10 p-4 text-success"
        >
          <p class="font-medium">{{ $t('views.LibraryPipelineWizard.pipeline_created') }}</p>
          <p class="text-sm mt-1">
            {{ $t('views.LibraryPipelineWizard.pipeline_ready', { name: result.name }) }}
             <a :href="`/pipelines/${result.id}`" class="underline font-medium" data-testid="library-wizard-view-pipeline">{{ $t('views.LibraryPipelineWizard.view_pipeline') }}</a>
          </p>
        </div>

        <div
          v-if="createError"
          class="mt-6 rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive"
        >
          {{ createError }}
        </div>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import PageHeader from '../components/shared/PageHeader.vue'
import Button from 'primevue/button'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import OwnershipPicker from '../components/OwnershipPicker.vue'
import type { OwnershipValue } from '../components/OwnershipPicker.vue'
import BackLink from '../components/BackLink.vue'
import { ArrowLeft } from '@lucide/vue'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import { layoutNodes } from '../utils/graph-layout'
import { api } from '../lib/api/client'

interface LibraryPrimitive {
  id: string
  primitive_type: string
  name: string
  description: string | null
  author: string
  version: string
  tags: string[]
  visibility: string
  content_json: {
    agents?: Array<{
      name: string
      description?: string
      prompt_template?: string
      connector_type_refs?: Array<{ connector_type: string }>
    }>
    graph_nodes?: Array<Record<string, unknown>>
    edges?: Array<Record<string, unknown>>
    sub_pipeline_graph_json?: {
      nodes: Array<{ id: string; node_type?: string; label?: string }>
      edges: Array<{ source: string; target: string; edge_type?: string }>
    }
  }
}

interface CreatePipelineResponse {
  id: string
  name: string
  description: string | null
  template_source_id: string
  agent_count: number
  edge_count: number
  ready_to_run: boolean
  created_at: string
  updated_at: string
}

const route = useRoute()
const router = useRouter()
const { t } = useI18n()

const primitiveId = route.params.id as string
const pipelineName = ref('')
const pipelineDescription = ref('')
const ownership = ref<OwnershipValue>({ owner_team_id: null, visibility: 'org' })
const creating = ref(false)
const createError = ref<string | null>(null)
const result = ref<CreatePipelineResponse | null>(null)

const templateAgents = ref<Array<{ name: string; description?: string; connector_type_refs?: Array<{ connector_type: string }> }>>([])

const { loading, error, data: primitive, load: _reload } = useDataFetch<LibraryPrimitive>(
  () => api.GET('/api/v1/libraries/{primitive_id}', {
    params: { path: { primitive_id: primitiveId } },
  }),
  { immediate: true },
)

watch(primitive, (p) => {
  if (p) {
    templateAgents.value = p.content_json?.agents ?? []
    pipelineName.value = `${p.name}`
    pipelineDescription.value = p.description ?? ''
  }
}, { immediate: true })

const subGraphNodes = computed(() => {
  const sub = primitive.value?.content_json?.sub_pipeline_graph_json
  if (!sub) return []
  return layoutNodes(sub.nodes, sub.edges)
})

const subGraphEdges = computed(() => {
  const sub = primitive.value?.content_json?.sub_pipeline_graph_json
  if (!sub) return []
  const usedIds = new Set(subGraphNodes.value.map((n: { id: string }) => n.id))
  return sub.edges
    .filter(e => usedIds.has(e.source) && usedIds.has(e.target))
    .map(e => ({
      id: `${e.source}-${e.target}`,
      source: e.source,
      target: e.target,
      type: 'smoothstep',
    }))
})

const subNodeTypes = { agent: 'agent' }

async function createPipeline() {
  creating.value = true
  createError.value = null
  try {
    const { data } = await api.POST('/api/v1/libraries/{primitive_id}/create-pipeline', {
      params: { path: { primitive_id: primitiveId } },
      body: {
        name: pipelineName.value || undefined,
        description: pipelineDescription.value || undefined,
        owner_team_id: ownership.value.owner_team_id,
        visibility: ownership.value.visibility,
      },
    })
    result.value = data as unknown as CreatePipelineResponse
  } catch (e) {
    createError.value = e instanceof Error ? e.message : t('views.LibraryPipelineWizard.failed_to_create_pipeline')
  } finally {
    creating.value = false
  }
}
</script>
