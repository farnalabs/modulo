<template>
  <div class="min-h-screen bg-gray-50">
    <header class="bg-white border-b border-gray-200 px-6 py-4">
      <div class="max-w-6xl mx-auto flex items-center justify-between">
        <h1 class="text-xl font-semibold text-gray-900">Library</h1>
        <div class="flex items-center gap-3">
          <input
            v-model="search"
            type="text"
            placeholder="Search primitives..."
            class="px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            @input="loadPrimitives"
          />
          <select
            v-model="typeFilter"
            class="px-3 py-1.5 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            @change="loadPrimitives"
          >
            <option value="">All Types</option>
            <option value="pipeline_template">Pipeline Templates</option>
            <option value="workflow">Workflows</option>
            <option value="agent">Agents</option>
            <option value="schema">Schemas</option>
            <option value="integration">Integrations</option>
            <option value="test_fixture">Test Fixtures</option>
          </select>
        </div>
      </div>
    </header>

    <main class="max-w-6xl mx-auto px-6 py-8">
      <div v-if="loading" class="text-center py-12 text-gray-500">Loading...</div>

      <div v-else-if="error" class="bg-red-50 border border-red-200 text-red-700 rounded-lg p-4 mb-6">
        {{ error }}
      </div>

      <div v-else-if="primitives.length === 0" class="text-center py-12 text-gray-500">
        No primitives found.
      </div>

      <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="prim in primitives"
          :key="prim.id"
          class="bg-white border border-gray-200 rounded-lg p-5 hover:shadow-md transition-shadow"
        >
          <div class="flex items-start justify-between mb-3">
            <div>
              <span
                class="inline-block px-2 py-0.5 text-xs font-medium rounded-full"
                :class="typeBadgeClass(prim.primitive_type)"
              >
                {{ prim.primitive_type }}
              </span>
              <h3 class="mt-2 text-base font-medium text-gray-900">{{ prim.name }}</h3>
            </div>
            <div v-if="prim.visibility === 'community'" class="text-xs text-purple-600 font-medium bg-purple-50 px-2 py-0.5 rounded">
              Community
            </div>
          </div>

          <p v-if="prim.description" class="text-sm text-gray-600 mb-4 line-clamp-2">
            {{ prim.description }}
          </p>

          <div class="flex items-center gap-2 flex-wrap mb-4">
            <span
              v-for="tag in (prim.tags || []).slice(0, 3)"
              :key="tag"
              class="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded"
            >
              {{ tag }}
            </span>
            <span v-if="(prim.tags || []).length > 3" class="text-xs text-gray-400">
              +{{ prim.tags.length - 3 }}
            </span>
          </div>

          <div class="flex items-center gap-2">
            <button
              v-if="prim.primitive_type === 'pipeline_template'"
              class="flex-1 px-3 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
              @click="createPipeline(prim)"
            >
              Create Pipeline
            </button>
            <button
              class="flex-1 px-3 py-2 border border-gray-300 text-gray-700 text-sm font-medium rounded-lg hover:bg-gray-50 transition-colors"
              @click="viewPrimitive(prim)"
            >
              View Details
            </button>
          </div>
        </div>
      </div>

      <div v-if="total > pageSize" class="flex justify-center gap-2 mt-8">
        <button
          :disabled="page <= 1"
          class="px-4 py-2 text-sm border border-gray-300 rounded-lg disabled:opacity-50 hover:bg-gray-50"
          @click="prevPage"
        >
          Previous
        </button>
        <span class="px-4 py-2 text-sm text-gray-600">
          Page {{ page }} of {{ Math.ceil(total / pageSize) }}
        </span>
        <button
          :disabled="page >= Math.ceil(total / pageSize)"
          class="px-4 py-2 text-sm border border-gray-300 rounded-lg disabled:opacity-50 hover:bg-gray-50"
          @click="nextPage"
        >
          Next
        </button>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'

interface LibraryPrimitive {
  id: string
  organisation_id: string
  source: string
  primitive_type: string
  name: string
  slug: string
  description: string | null
  author: string
  version: string
  tags: string[]
  visibility: string
  created_at: string
  updated_at: string
}

interface ListResponse {
  items: LibraryPrimitive[]
  total: number
  page: number
  page_size: number
}

const router = useRouter()
const { get } = useApi()

const primitives = ref<LibraryPrimitive[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const search = ref('')
const typeFilter = ref('')
const page = ref(1)
const pageSize = ref(12)
const total = ref(0)

async function loadPrimitives() {
  loading.value = true
  error.value = null
  try {
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: String(pageSize.value),
    })
    if (typeFilter.value) params.set('primitive_type', typeFilter.value)
    if (search.value) params.set('search', search.value)

    const data = await get<ListResponse>(`/api/v1/libraries?${params}`)
    primitives.value = data.items
    total.value = data.total
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load primitives'
  } finally {
    loading.value = false
  }
}

function prevPage() {
  if (page.value > 1) {
    page.value--
    loadPrimitives()
  }
}

function nextPage() {
  if (page.value < Math.ceil(total.value / pageSize.value)) {
    page.value++
    loadPrimitives()
  }
}

function typeBadgeClass(type: string): string {
  const map: Record<string, string> = {
    pipeline_template: 'bg-blue-50 text-blue-700',
    workflow: 'bg-green-50 text-green-700',
    agent: 'bg-purple-50 text-purple-700',
    schema: 'bg-amber-50 text-amber-700',
    integration: 'bg-cyan-50 text-cyan-700',
    test_fixture: 'bg-pink-50 text-pink-700',
  }
  return map[type] ?? 'bg-gray-50 text-gray-700'
}

function createPipeline(prim: LibraryPrimitive) {
  router.push({ name: 'library-pipeline-wizard', params: { id: prim.id } })
}

function viewPrimitive(prim: LibraryPrimitive) {
  router.push({ name: 'library-pipeline-wizard', params: { id: prim.id } })
}

onMounted(loadPrimitives)
</script>
