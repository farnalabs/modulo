<template>
  <FeatureGate feature-name="plugin_management" required-tier="community" show-disabled>

    <div class="page-wide">
    <header class="flex items-center justify-between">
      <PageHeader title="Node Categories" subtitle="Manage categories for classifying nodes in pipelines" />
      <Button class="border-primary/30 hover:border-primary/60" data-testid="admin-node-categories-add" @click="openAddForm">
        Add Category
      </Button>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadCategories" />

    <template v-else>
      <div v-if="editorMode === 'add'" class="card p-6">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.AdminNodeCategoriesView.new_node_category') }}</h2>
        <NodeCategoryEditor
          :category="null"
          @saved="onCategoryCreated"
          @cancelled="closeEditor"
        />
      </div>

      <div v-if="categories.length === 0 && editorMode === null" class="card p-8 text-center">
        <p class="text-lg font-medium">{{ $t('views.AdminNodeCategoriesView.no_node_categories_configured') }}</p>
        <p class="mt-1 text-sm text-muted-foreground">
          Add a category to classify and organize nodes in your pipelines.
        </p>
      </div>

      <div v-if="categories.length > 0" class="overflow-hidden rounded-lg border">
        <table class="w-full text-left text-sm">
          <thead class="bg-muted/50">
            <tr>
              <th class="px-4 py-3 font-medium">{{ $t('views.AdminNodeCategoriesView.name') }}</th>
              <th class="px-4 py-3 font-medium">{{ $t('views.AdminNodeCategoriesView.description') }}</th>
              <th class="px-4 py-3 font-medium">{{ $t('views.AdminNodeCategoriesView.color') }}</th>
              <th class="px-4 py-3 font-medium">{{ $t('views.AdminNodeCategoriesView.icon') }}</th>
              <th class="px-4 py-3 font-medium">{{ $t('views.AdminNodeCategoriesView.sort_order') }}</th>
              <th class="px-4 py-3 font-medium text-right">{{ $t('views.AdminNodeCategoriesView.actions') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="cat in categories"
              :key="cat.id"
              class="hover:bg-muted/30 transition-colors"
            >
              <td class="px-4 py-3 font-medium">{{ cat.name }}</td>
              <td class="px-4 py-3 text-muted-foreground">{{ cat.description || '—' }}</td>
              <td class="px-4 py-3">
                <span class="inline-flex items-center gap-2">
                  <span
                    class="inline-block h-5 w-5 rounded-full border border-border"
                    :style="{ backgroundColor: cat.color || '#6366f1' }"
                  />
                  <span class="font-mono text-xs text-muted-foreground">{{ cat.color || '#6366f1' }}</span>
                </span>
              </td>
              <td class="px-4 py-3">
                <span v-if="cat.icon" class="inline-flex items-center gap-1.5">
                  <span class="h-4 w-4 text-muted-foreground" v-html="iconSvg(cat.icon)" />
                  {{ cat.icon }}
                </span>
                <span v-else class="text-muted-foreground">—</span>
              </td>
              <td class="px-4 py-3">{{ cat.sort_order }}</td>
              <td class="px-4 py-3 text-right">
                <TableActions :actions="categoryActions(cat)" />
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="editCategoryId" class="card p-6">
        <h2 class="mb-4 text-base font-semibold">{{ $t('views.AdminNodeCategoriesView.edit_node_category') }}</h2>
        <NodeCategoryEditor
          :category="editingCategory"
          @saved="onCategoryUpdated"
          @cancelled="closeEditor"
        />
      </div>

      <div v-if="deleteConfirmCategoryId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
        <p class="text-sm font-medium text-destructive">Delete "{{ deleteConfirmName }}"?</p>
        <p class="mt-1 text-sm text-destructive/80">{{ $t('views.AdminNodeCategoriesView.this_action_cannot_be_undone') }}</p>
        <div class="mt-3 flex items-center gap-2">
          <Button :disabled="deleting" severity="danger" data-testid="admin-node-categories-delete-confirm" @click="deleteCategory">
            {{ deleting ? 'Deleting...' : 'Delete' }}
          </Button>
          <button type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="admin-node-categories-delete-cancel"
            @click="deleteConfirmCategoryId = null"
          >
            Cancel
          </button>
        </div>
        <div v-if="deleteError" class="mt-2 text-sm text-destructive">{{ deleteError }}</div>
      </div>
    </template>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import NodeCategoryEditor from '../components/NodeCategoryEditor.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import Button from 'primevue/button'
import TableActions from '../components/shared/TableActions.vue'

const planStore = usePlanStore()

interface NodeCategory {
  id: string
  name: string
  description: string | null
  color: string
  icon: string | null
  sort_order: number
}

const { data: categoriesData, loading, error, load: loadCategories } = useDataFetch(
  () => api.GET('/api/v1/node-categories') as Promise<{ data?: { items?: NodeCategory[] }; error?: { detail?: string } }>,
  { initialValue: { items: [] as NodeCategory[] } }
)

const categories = computed(() => {
  const d = categoriesData.value
  return (d as any)?.items ?? d ?? []
})

const editorMode = ref<'add' | 'edit' | null>(null)
const editCategoryId = ref<string | null>(null)
const editingCategory = ref<NodeCategory | null>(null)

const deleteConfirmCategoryId = ref<string | null>(null)
const deleteConfirmName = ref('')
const deleting = ref(false)
const deleteError = ref<string | null>(null)

function openAddForm() {
  editorMode.value = 'add'
  editCategoryId.value = null
  editingCategory.value = null
  deleteConfirmCategoryId.value = null
}

function openEditForm(cat: NodeCategory) {
  editorMode.value = 'edit'
  editCategoryId.value = cat.id
  editingCategory.value = { ...cat }
  deleteConfirmCategoryId.value = null
}

function closeEditor() {
  editorMode.value = null
  editCategoryId.value = null
  editingCategory.value = null
}

function onCategoryCreated() {
  closeEditor()
  loadCategories()
}

function onCategoryUpdated() {
  closeEditor()
  loadCategories()
}

function confirmDelete(cat: NodeCategory) {
  deleteConfirmCategoryId.value = cat.id
  deleteConfirmName.value = cat.name
  editorMode.value = null
  deleteError.value = null
}

async function deleteCategory() {
  if (!deleteConfirmCategoryId.value) return
  deleting.value = true
  deleteError.value = null
  try {
    const { error: err, response } = await api.DELETE('/api/v1/node-categories/{category_id}', {
      params: { path: { category_id: deleteConfirmCategoryId.value } },
    })
    if (err) {
      deleteError.value = String(err)
    } else if (response.status === 204 || response.ok) {
      deleteConfirmCategoryId.value = null
      await loadCategories()
    }
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  } finally {
    deleting.value = false
  }
}

const iconSvgs: Record<string, string> = {
  bot: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8" y2="16"/><line x1="16" y1="16" x2="16" y2="16"/></svg>',
  database: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
  globe: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
  mail: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>',
  'message-circle': '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>',
  'refresh-cw': '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
  search: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
  settings: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  sliders: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
  terminal: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
  upload: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>',
  zap: '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
}

function iconSvg(name: string): string {
  return iconSvgs[name] || ''
}

function categoryActions(cat: NodeCategory) {
  return [
    {
      key: 'edit',
      label: 'Edit',
      onClick: () => openEditForm(cat),
    },
    {
      key: 'delete',
      label: 'Delete',
      onClick: () => confirmDelete(cat),
      danger: true,
    },
  ]
}

planStore.fetchPlan()
</script>
