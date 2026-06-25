<template>
  <div class="min-h-screen bg-gray-50">
    <header class="bg-white border-b border-gray-200 px-6 py-4">
      <div class="max-w-3xl mx-auto">
        <button
          class="text-sm text-gray-600 hover:text-gray-900 mb-2 inline-flex items-center gap-1"
          @click="$router.push({ name: 'library' })"
        >
          &larr; Back to Library
        </button>
        <h1 class="text-xl font-semibold text-gray-900">Create Pipeline from Template</h1>
      </div>
    </header>

    <main class="max-w-3xl mx-auto px-6 py-8">
      <div v-if="loading" class="text-center py-12 text-gray-500">Loading template...</div>

      <div v-else-if="error" class="bg-red-50 border border-red-200 text-red-700 rounded-lg p-4 mb-6">
        {{ error }}
      </div>

      <div v-else>
        <div class="bg-white border border-gray-200 rounded-lg p-6 mb-6">
          <h2 class="text-lg font-medium text-gray-900 mb-1">{{ primitive?.name }}</h2>
          <p v-if="primitive?.description" class="text-sm text-gray-600 mb-4">
            {{ primitive.description }}
          </p>

          <div class="flex items-center gap-2 mb-4">
            <span
              v-for="tag in (primitive?.tags || []).slice(0, 5)"
              :key="tag"
              class="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded"
            >
              {{ tag }}
            </span>
          </div>

          <div class="bg-gray-50 rounded-lg p-4 text-sm text-gray-700">
            <p><strong>Author:</strong> {{ primitive?.author }}</p>
            <p><strong>Version:</strong> {{ primitive?.version }}</p>
          </div>
        </div>

        <div class="bg-white border border-gray-200 rounded-lg p-6 mb-6">
          <h3 class="text-base font-medium text-gray-900 mb-4">Pipeline Configuration</h3>

          <div class="space-y-4">
            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Pipeline Name</label>
              <input
                v-model="pipelineName"
                type="text"
                class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                :placeholder="`${primitive?.name ?? 'Pipeline'} (from template)`"
              />
            </div>

            <div>
              <label class="block text-sm font-medium text-gray-700 mb-1">Description</label>
              <textarea
                v-model="pipelineDescription"
                rows="3"
                class="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                :placeholder="primitive?.description ?? 'Pipeline created from library template'"
              />
            </div>
          </div>
        </div>

        <div v-if="templateAgents.length > 0" class="bg-white border border-gray-200 rounded-lg p-6 mb-6">
          <h3 class="text-base font-medium text-gray-900 mb-4">Template Agents ({{ templateAgents.length }})</h3>
          <div class="space-y-3">
            <div
              v-for="(agent, i) in templateAgents"
              :key="i"
              class="flex items-start gap-3 p-3 bg-gray-50 rounded-lg"
            >
              <div class="w-6 h-6 rounded-full bg-blue-100 text-blue-700 flex items-center justify-center text-xs font-medium shrink-0">
                {{ i + 1 }}
              </div>
              <div>
                <p class="text-sm font-medium text-gray-900">{{ agent.name }}</p>
                <p v-if="agent.description" class="text-xs text-gray-600 mt-0.5">{{ agent.description }}</p>
                <div class="flex gap-2 mt-1">
                  <span
                    v-for="ref in (agent.connector_type_refs || [])"
                    :key="ref.connector_type"
                    class="text-xs bg-purple-50 text-purple-700 px-1.5 py-0.5 rounded"
                  >
                    {{ ref.connector_type }}
                  </span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="flex items-center gap-3">
          <button
            :disabled="creating"
            class="px-6 py-2.5 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
            @click="createPipeline"
          >
            {{ creating ? 'Creating...' : 'Create Pipeline' }}
          </button>
          <button
            class="px-6 py-2.5 border border-gray-300 text-gray-700 text-sm font-medium rounded-lg hover:bg-gray-50 transition-colors"
            @click="$router.push({ name: 'library' })"
          >
            Cancel
          </button>
        </div>

        <div
          v-if="result"
          class="mt-6 bg-green-50 border border-green-200 text-green-700 rounded-lg p-4"
        >
          <p class="font-medium">Pipeline created!</p>
          <p class="text-sm mt-1">
            {{ result.name }} is ready. 
            <a :href="`/pipelines/${result.id}`" class="underline font-medium">View pipeline</a>
          </p>
        </div>

        <div
          v-if="createError"
          class="mt-6 bg-red-50 border border-red-200 text-red-700 rounded-lg p-4"
        >
          {{ createError }}
        </div>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useApi } from '../composables/useApi'

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
const { get, post } = useApi()

const primitiveId = route.params.id as string
const primitive = ref<LibraryPrimitive | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const pipelineName = ref('')
const pipelineDescription = ref('')
const creating = ref(false)
const createError = ref<string | null>(null)
const result = ref<CreatePipelineResponse | null>(null)

const templateAgents = ref<Array<{ name: string; description?: string; connector_type_refs?: Array<{ connector_type: string }> }>>([])

onMounted(async () => {
  try {
    const data = await get<LibraryPrimitive>(`/api/v1/libraries/${primitiveId}`)
    primitive.value = data
    templateAgents.value = data.content_json?.agents ?? []
    pipelineName.value = `${data.name}`
    pipelineDescription.value = data.description ?? ''
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load template'
  } finally {
    loading.value = false
  }
})

async function createPipeline() {
  creating.value = true
  createError.value = null
  try {
    const data = await post<CreatePipelineResponse>(
      `/api/v1/libraries/${primitiveId}/create-pipeline`,
      {
        name: pipelineName.value || undefined,
        description: pipelineDescription.value || undefined,
      },
    )
    result.value = data
  } catch (e) {
    createError.value = e instanceof Error ? e.message : 'Failed to create pipeline'
  } finally {
    creating.value = false
  }
}
</script>
