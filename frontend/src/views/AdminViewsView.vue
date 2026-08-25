<template>
  <div class="page-wide">
    <FeatureGate feature-name="view_modes" required-tier="team" show-disabled>
    <header class="flex items-center justify-between">
      <PageHeader :title="$t('components.ViewToggle.saved_views')" :subtitle="$t('views.AdminViewsView.manage_saved_views_for_organizing_and_filtering_data')" />
      <Button class="border-primary/30 hover:border-primary/60" data-testid="admin-views-add" @click="openAddForm">
        Create View
      </Button>
    </header>
    <LoadingSpinner v-if="loading" />
    <div v-else-if="error" data-testid="admin-views-error">
      <ErrorAlert :message="error" :on-retry="loadViews" />
    </div>
    <template v-else>
      <div v-if="showForm" class="card p-6">
        <h2 class="mb-4 text-base font-semibold" data-testid="admin-views-form-title">{{ editingId ? 'Edit View' : 'New View' }}</h2>
        <form class="space-y-4" @submit.prevent="handleSave">
          <div>
            <label for="adminviewsview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.AdminViewsView.name') }}</label>
            <input id="adminviewsview-field-6"
              v-model="form.name"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
              :placeholder="$t('views.AdminViewsView.my_view')"
              data-testid="admin-views-name-input"
              required
            />
          </div>
          <div>
            <label for="adminviewsview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.AdminViewsView.view_type') }}</label>
            <Select
  aria-label="View type"
  v-model="form.view_type"
  placeholder="table"
  data-testid="admin-views-type-select"
  class="w-full"
  :options="[{ value: 'table', label: $t('views.AdminViewsView.table') }, { value: 'grid', label: $t('views.AdminViewsView.grid') }, { value: 'kanban', label: $t('views.AdminViewsView.kanban') }, { value: 'timeline', label: $t('views.AdminViewsView.timeline') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
          <div>
            <label for="adminviewsview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.AdminViewsView.filters_json') }}</label>
            <textarea id="adminviewsview-field-4"
              v-model="form.filters"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/50"
              rows="4"
              placeholder='{"status": "active"}'
              data-testid="admin-views-filters-input"
            />
          </div>
          <div>
            <label for="adminviewsview-field-3" class="mb-1 block text-sm font-medium">{{ $t('views.AdminViewsView.columns') }}</label>
            <input id="adminviewsview-field-3"
              v-model="form.columns"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
              :placeholder="$t('views.AdminViewsView.name_status_createdat')"
              data-testid="admin-views-columns-input"
            />
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div>
              <label for="adminviewsview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.AdminViewsView.sort_by') }}</label>
              <input id="adminviewsview-field-2"
                v-model="form.sort_by"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
                placeholder="created_at"
                data-testid="admin-views-sort-by-input"
              />
            </div>
            <div>
              <label for="adminviewsview-field-1" class="mb-1 block text-sm font-medium">{{ $t('components.NodeCategoryEditor.sort_order') }}</label>
            <Select
  aria-label="Sort order"
  v-model="form.sort_order"
  placeholder="desc"
  data-testid="admin-views-sort-order-select"
  class="w-full"
  :options="[{ value: 'desc', label: $t('views.AdminViewsView.descending') }, { value: 'asc', label: $t('views.AdminViewsView.ascending') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
            </div>
          </div>
          <div v-if="saveError" class="text-sm text-destructive">{{ saveError }}</div>
          <div class="flex items-center gap-2">
            <Button type="submit" :disabled="saving" class="border-primary/30 hover:border-primary/60" data-testid="admin-views-save">
              {{ saving ? 'Saving...' : 'Save' }}
            </Button>
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
              data-testid="admin-views-cancel"
              @click="closeForm"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
      <div v-if="views.length === 0 && !showForm" class="card p-8 text-center">
        <svg
          class="mx-auto h-16 w-16 text-muted-foreground/40"
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.5"
        >
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <line x1="3" y1="9" x2="21" y2="9" />
          <line x1="9" y1="21" x2="9" y2="9" />
        </svg>
        <p class="mt-4 text-lg font-medium">{{ $t('views.AdminViewsView.no_saved_views_yet') }}</p>
        <p class="mt-1 text-sm text-muted-foreground max-w-md mx-auto">
          Create a view to save filter configurations and layout preferences so you can quickly switch between different data perspectives.
        </p>
        <a
          href="https://modulo.run/docs/features/saved-views"
          target="_blank"
          class="mt-4 inline-flex items-center gap-1 text-sm text-primary hover:underline"
        >
          Learn about saved views
          <svg class="h-3.5 w-3.5" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
            <polyline points="15 3 21 3 21 9" />
            <line x1="10" y1="14" x2="21" y2="3" />
          </svg>
        </a>
      </div>
      <div v-if="views.length > 0" class="overflow-hidden rounded-lg border">
        <table class="w-full text-left text-sm">
          <thead class="bg-muted/50">
            <tr>
              <th class="px-4 py-3 font-medium">{{ $t('views.AdminViewsView.name') }}</th>
              <th class="px-4 py-3 font-medium">{{ $t('views.AdminViewsView.type') }}</th>
              <th class="px-4 py-3 font-medium">{{ $t('views.AdminViewsView.filters') }}</th>
              <th class="px-4 py-3 font-medium">{{ $t('views.AdminViewsView.created_by') }}</th>
              <th class="px-4 py-3 font-medium">{{ $t('views.AdminViewsView.created_at') }}</th>
              <th class="px-4 py-3 font-medium text-right">{{ $t('views.AdminViewsView.actions') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="v in views"
              :key="v.id"
              class="hover:bg-muted/30 transition-colors"
            >
              <td class="px-4 py-3 font-medium">{{ v.name }}</td>
              <td class="px-4 py-3">
                <span class="inline-flex rounded-full bg-primary/10 px-2.5 py-0.5 text-xs font-medium text-primary capitalize">{{ v.view_type }}</span>
              </td>
              <td class="px-4 py-3 text-muted-foreground max-w-[200px] truncate font-mono text-xs" v-tooltip.top="filtersSummary(v.filters)">{{ filtersSummary(v.filters) }}</td>
              <td class="px-4 py-3 text-muted-foreground">{{ v.created_by || '—' }}</td>
              <td class="px-4 py-3 text-muted-foreground">{{ formatDate(v.created_at) }}</td>
              <td class="px-4 py-3 text-right">
                <TableActions :actions="viewActions(v)" />
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="deleteConfirmId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
        <p class="text-sm font-medium text-destructive">Delete "{{ deleteConfirmName }}"?</p>
        <p class="mt-1 text-sm text-destructive/80">{{ $t('views.AdminModelBackendsView.this_action_cannot_be_undone') }}</p>
        <div class="mt-3 flex items-center gap-2">
          <Button :disabled="deleting" severity="danger" data-testid="admin-views-delete-confirm" @click="deleteView">
            {{ deleting ? 'Deleting...' : 'Delete' }}
          </Button>
          <button type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            data-testid="admin-views-delete-cancel"
            @click="deleteConfirmId = null"
          >
            Cancel
          </button>
        </div>
        <div v-if="deleteError" class="mt-2 text-sm text-destructive">{{ deleteError }}</div>
      </div>
    </template>
    </FeatureGate>
  </div>
</template>
<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import { ref, computed } from 'vue'
import { getAccessToken } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { formatDateShort } from '../lib/formatDate'
import { formatApiError } from '../lib/api/formatError'
import Button from 'primevue/button'
import Select from 'primevue/select'
import TableActions from '../components/shared/TableActions.vue'

interface SavedView {
  id: string
  name: string
  view_type: string
  filters: Record<string, unknown> | string | null
  columns: string[] | null
  sort_by: string | null
  sort_order: string
  created_by: string | null
  created_at: string
}

const { data: viewsData, loading, error, load: loadViews } = useDataFetch(
  async () => {
    try {
      const res = await fetch('/api/v1/views', { headers: getHeaders() })
      if (!res.ok) {
        const errData = await res.json().catch(() => null)
        return { error: { detail: errData?.detail ?? `Failed to load views (${res.status})` } }
      }
      const data = await res.json()
      return { data: (data.items ?? data) as SavedView[] }
    } catch (e: unknown) {
      return { error: { detail: formatApiError(e) } }
    }
  },
  { initialValue: [] as SavedView[] },
)

const views = computed(() => viewsData.value ?? [])

const showForm = ref(false)
const editingId = ref<string | null>(null)
const saving = ref(false)
const saveError = ref<string | null>(null)
const form = ref({
  name: '',
  view_type: 'table',
  filters: '',
  columns: '',
  sort_by: '',
  sort_order: 'desc',
})

const deleteConfirmId = ref<string | null>(null)
const deleteConfirmName = ref('')
const deleting = ref(false)
const deleteError = ref<string | null>(null)

function getHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getAccessToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

function filtersSummary(filters: SavedView['filters']): string {
  if (!filters) return '—'
  const str = typeof filters === 'string' ? filters : JSON.stringify(filters)
  return str.length > 60 ? str.slice(0, 60) + '…' : str
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '—'
  try {
    return formatDateShort(new Date(dateStr))
  } catch {
    return dateStr
  }
}

function openAddForm() {
  editingId.value = null
  form.value = { name: '', view_type: 'table', filters: '', columns: '', sort_by: '', sort_order: 'desc' }
  showForm.value = true
  deleteConfirmId.value = null
  saveError.value = null
  error.value = null
}

function openEditForm(v: SavedView) {
  editingId.value = v.id
  form.value = {
    name: v.name,
    view_type: v.view_type,
    filters: v.filters ? (typeof v.filters === 'string' ? v.filters : JSON.stringify(v.filters, null, 2)) : '',
    columns: v.columns?.join(', ') || '',
    sort_by: v.sort_by || '',
    sort_order: v.sort_order || 'desc',
  }
  showForm.value = true
  deleteConfirmId.value = null
  saveError.value = null
}

function closeForm() {
  showForm.value = false
  editingId.value = null
  saveError.value = null
}

async function handleSave() {
  saving.value = true
  saveError.value = null
  try {
    let filters: unknown = null
    if (form.value.filters.trim()) {
      try {
        filters = JSON.parse(form.value.filters)
      } catch {
        throw new Error('Filters must be valid JSON')
      }
    }

    const columns = form.value.columns
      ? form.value.columns.split(',').map(c => c.trim()).filter(Boolean)
      : null
    const payload: Record<string, unknown> = {
      name: form.value.name,
      view_type: form.value.view_type,
      filters,
      columns,
      sort_by: form.value.sort_by || null,
      sort_order: form.value.sort_order,
    }

    const method = editingId.value ? 'PATCH' : 'POST'
    const url = editingId.value ? `/api/v1/views/${editingId.value}` : '/api/v1/views'

    const res = await fetch(url, {
      method,
      headers: getHeaders(),
      body: JSON.stringify(payload),
    })
    if (!res.ok) {
      const errData = await res.json().catch(() => null)
      throw new Error(errData?.detail ?? `Save failed (${res.status})`)
    }
    closeForm()
    await loadViews()
  } catch (e: unknown) {
    saveError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

function confirmDelete(v: SavedView) {
  deleteConfirmId.value = v.id
  deleteConfirmName.value = v.name
  showForm.value = false
  deleteError.value = null
}

async function deleteView() {
  if (!deleteConfirmId.value) return
  deleting.value = true
  deleteError.value = null
  try {
    const res = await fetch(`/api/v1/views/${deleteConfirmId.value}`, {
      method: 'DELETE',
      headers: getHeaders(),
    })
    if (!res.ok && res.status !== 204) {
      const errData = await res.json().catch(() => null)
      throw new Error(errData?.detail ?? `Delete failed (${res.status})`)
    }
    deleteConfirmId.value = null
    await loadViews()
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  } finally {
    deleting.value = false
  }
}

async function duplicateView(v: SavedView) {
  try {
    const payload: Record<string, unknown> = {
      name: `${v.name} (copy)`,
      view_type: v.view_type,
      filters: v.filters,
      columns: v.columns,
      sort_by: v.sort_by,
      sort_order: v.sort_order,
    }
    const res = await fetch('/api/v1/views', {
      method: 'POST',
      headers: getHeaders(),
      body: JSON.stringify(payload),
    })
    if (!res.ok) {
      const errData = await res.json().catch(() => null)
      throw new Error(errData?.detail ?? `Duplicate failed (${res.status})`)
    }
    await loadViews()
  } catch (e: unknown) {
    error.value = formatApiError(e)
  }
}

function viewActions(v: SavedView) {
  return [
    {
      key: 'duplicate',
      label: 'Duplicate',
      onClick: () => duplicateView(v),
    },
    {
      key: 'edit',
      label: 'Edit',
      onClick: () => openEditForm(v),
    },
    {
      key: 'delete',
      label: 'Delete',
      onClick: () => confirmDelete(v),
      danger: true,
    },
  ]
}

/* onMounted handled by useDataFetch */
</script>
