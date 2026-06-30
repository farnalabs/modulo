<template>
  <div class="space-y-4">
    <div>
      <label class="mb-1 block text-sm font-medium">Name</label>
      <input
        v-model="form.name"
        type="text"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        placeholder="e.g. LLM Call, Connector Read"
      />
    </div>

    <div>
      <label class="mb-1 block text-sm font-medium">Description</label>
      <textarea
        v-model="form.description"
        rows="3"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        placeholder="Optional description of this category"
      />
    </div>

    <div>
      <label class="mb-1 block text-sm font-medium">Color</label>
      <div class="flex items-center gap-3">
        <input
          v-model="form.color"
          type="color"
          class="h-9 w-14 cursor-pointer rounded border border-input bg-background p-0.5"
        />
        <input
          v-model="form.color"
          type="text"
          class="flex-1 rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          placeholder="#6366f1"
          pattern="^#[0-9a-fA-F]{6}$"
        />
      </div>
    </div>

    <div>
      <label class="mb-1 block text-sm font-medium">Icon</label>
      <select
        v-model="form.icon"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <option value="">None</option>
        <option value="bot">Bot</option>
        <option value="database">Database</option>
        <option value="globe">Globe</option>
        <option value="mail">Mail</option>
        <option value="message-circle">Message Circle</option>
        <option value="refresh-cw">Refresh</option>
        <option value="search">Search</option>
        <option value="settings">Settings</option>
        <option value="sliders">Sliders</option>
        <option value="terminal">Terminal</option>
        <option value="upload">Upload</option>
        <option value="zap">Zap</option>
      </select>
    </div>

    <div>
      <label class="mb-1 block text-sm font-medium">Sort Order</label>
      <input
        v-model.number="form.sort_order"
        type="number"
        min="0"
        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
    </div>

    <div v-if="error" class="text-sm text-destructive">{{ error }}</div>

    <div class="flex items-center gap-2">
      <button
        :disabled="!form.name.trim() || saving"
        class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        @click="save"
      >
        {{ saving ? 'Saving...' : isEditing ? 'Update Category' : 'Create Category' }}
      </button>
      <button
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
        @click="emit('cancelled')"
      >
        Cancel
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import { api } from '../lib/api/client'

export interface NodeCategoryForm {
  name: string
  description: string
  color: string
  icon: string
  sort_order: number
}

interface CategoryData {
  id?: string
  name?: string
  description?: string | null
  color?: string
  icon?: string | null
  sort_order?: number
}

const props = defineProps<{
  category?: CategoryData | null
}>()

const emit = defineEmits<{
  saved: [data: unknown]
  cancelled: []
}>()

const saving = ref(false)
const error = ref<string | null>(null)

const form = reactive<NodeCategoryForm>({
  name: '',
  description: '',
  color: '#6366f1',
  icon: '',
  sort_order: 0,
})

const isEditing = computed(() => !!props.category)

watch(
  () => props.category,
  (cat) => {
    if (cat) {
      form.name = cat.name ?? ''
      form.description = cat.description ?? ''
      form.color = cat.color ?? '#6366f1'
      form.icon = cat.icon ?? ''
      form.sort_order = cat.sort_order ?? 0
    }
  },
  { immediate: true },
)

async function save() {
  saving.value = true
  error.value = null

  const body = {
    name: form.name.trim(),
    description: form.description.trim() || null,
    color: form.color,
    icon: form.icon || null,
    sort_order: form.sort_order,
  }

  try {
    if (isEditing.value && props.category?.id) {
      const { data, error: err } = await api.PATCH('/api/v1/node-categories/{category_id}', {
        params: { path: { category_id: props.category.id } },
        body,
      })
      if (err) {
        throw new Error(String(err))
      }
      if (data) {
        emit('saved', data)
      }
    } else {
      const { data, error: err } = await api.POST('/api/v1/node-categories', {
        body,
      })
      if (err) {
        throw new Error(String(err))
      }
      if (data) {
        emit('saved', data)
      }
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'An unexpected error occurred'
  } finally {
    saving.value = false
  }
}
</script>
