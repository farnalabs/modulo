<template>
  <div class="mx-auto max-w-5xl px-4 py-8">
    <div class="mb-6 flex items-center justify-between">
      <div>
        <h1 class="text-2xl font-semibold tracking-tight">Lifecycle Maps</h1>
        <p class="mt-1 text-sm text-muted-foreground">
          Visual maps of your software delivery lifecycle stages and their transitions
        </p>
      </div>
      <Button
        variant="default"
        class="cursor-pointer"
        @click="handleCreate"
      >
        <PlusIcon class="mr-1.5 inline-block h-4 w-4" />
        New Map
      </Button>
    </div>

    <div v-if="loading" class="flex justify-center py-12">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-center text-destructive">
      {{ error }}
    </div>

    <div v-else-if="(maps ?? []).length === 0" class="rounded-lg border border-dashed border-border bg-card py-12 text-center">
      <MapIcon class="mx-auto mb-3 h-10 w-10 text-muted-foreground/50" />
      <h3 class="text-sm font-medium">No lifecycle maps yet</h3>
      <p class="mt-1 text-xs text-muted-foreground">
        Create your first lifecycle map to visualize your delivery pipeline
      </p>
      <Button
        variant="default"
        class="mt-4 cursor-pointer"
        @click="handleCreate"
      >
        Create Lifecycle Map
      </Button>
    </div>

    <div v-else class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
        v-for="map in (maps ?? [])"
        :key="map.id"
        class="cursor-pointer rounded-lg border bg-card p-4 shadow-sm transition-shadow hover:shadow-md"
        @click="openMap(map.id)"
      >
        <h3 class="text-sm font-semibold">{{ map.name }}</h3>
        <p v-if="map.description" class="mt-1 line-clamp-2 text-xs text-muted-foreground">
          {{ map.description }}
        </p>
        <div class="mt-3 flex items-center gap-3 text-[10px] text-muted-foreground">
          <span>Updated {{ formatDate(map.updated_at) }}</span>
        </div>
      </div>
    </div>

    <!-- Create dialog -->
    <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
      v-if="showCreateDialog"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      @click.self="showCreateDialog = false"
    >
      <div class="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
        <h3 class="mb-4 text-base font-semibold">Create Lifecycle Map</h3>
        <div class="space-y-4">
          <div>
            <label for="lifecyclemapsview-field-2" class="mb-1 block text-sm font-medium">Name</label>
            <input id="lifecyclemapsview-field-2"
              v-model="newName"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="My Delivery Lifecycle"
            />
          </div>
          <div>
            <label for="lifecyclemapsview-field-1" class="mb-1 block text-sm font-medium">Description</label>
            <textarea id="lifecyclemapsview-field-1"
              v-model="newDescription"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              rows="3"
              placeholder="Optional description"
            />
          </div>
          <div v-if="createError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ createError }}
          </div>
          <div class="flex justify-end gap-2">
            <button
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showCreateDialog = false"
            >
              Cancel
            </button>
            <Button
              :disabled="!newName.trim() || creating"
              variant="default"
              @click="handleCreateConfirm"
            >
              {{ creating ? 'Creating...' : 'Create' }}
            </Button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Plus as PlusIcon, Map as MapIcon } from '@lucide/vue'
import { useDataFetch } from '../../composables/useDataFetch'
import { formatApiError } from '../../lib/api/formatError'
import type { LifecycleMap } from '../../types/lifecycleMap'
import { useApi } from '../../composables/useApi'
import { formatDateShort } from '../../lib/formatDate'
import { Button } from '@/components/ui/button'

const router = useRouter()
const route = useRoute()
const { get, post } = useApi()

onMounted(() => {
  if (route.query.create === 'true') {
    handleCreate()
  }
})

const { loading, error, data: maps } = useDataFetch<LifecycleMap[]>(
  async () => ({ data: await get<LifecycleMap[]>('/api/v1/lifecycle-maps') }),
  { initialValue: [] as LifecycleMap[] },
)

const showCreateDialog = ref(false)
const newName = ref('')
const newDescription = ref('')
const creating = ref(false)
const createError = ref<string | null>(null)

function openMap(id: string) {
  router.push({ name: 'lifecycle-map-editor', params: { id } })
}

function handleCreate() {
  newName.value = ''
  newDescription.value = ''
  createError.value = null
  showCreateDialog.value = true
}

async function handleCreateConfirm() {
  if (!newName.value.trim()) return
  creating.value = true
  createError.value = null
  try {
    const data = await post<LifecycleMap>('/api/v1/lifecycle-maps', {
        name: newName.value.trim(),
        description: newDescription.value.trim() || null,
    })
    showCreateDialog.value = false
    if (data) router.push({ name: 'lifecycle-map-editor', params: { id: (data as LifecycleMap).id } })
  } catch (e: unknown) {
    createError.value = formatApiError(e)
  } finally {
    creating.value = false
  }
}

function formatDate(dateStr: string) {
  try {
    const d = new Date(dateStr)
    if (isNaN(d.getTime())) return '?'
    return formatDateShort(d)
  } catch {
    return '?'
  }
}
</script>
