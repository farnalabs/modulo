<template>
  <div class="mx-auto max-w-5xl space-y-8 p-6">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-3xl font-bold tracking-tight">Saved Views</h1>
        <p class="mt-1 text-muted-foreground">Manage saved views for organizing and filtering data</p>
      </div>
      <button
        class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground border border-primary/30 hover:border-primary/60 hover:brightness-110 transition-all duration-150"
        data-testid="admin-views-add"
        @click="openAddForm"
      >
        Create View
      </button>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadViews" />

    <template v-else>
      <div v-if="showForm" class="card p-6">
        <h2 class="mb-4 text-lg font-semibold">{{ editingId ? 'Edit View' : 'New View' }}</h2>
        <form class="space-y-4" @submit.prevent="handleSave">
          <div>
            <label class="mb-1 block text-sm font-medium">Name</label>
            <input
              v-model="form.name"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
              placeholder="My View"
              data-testid="admin-views-name-input"
              required
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">View Type</label>
            <select
              v-model="form.view_type"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
              data-testid="admin-views-type-select"
            >
              <option value="table">Table</option>
              <option value="grid">Grid</option>
              <option value="kanban">Kanban</option>
              <option value="timeline">Timeline</option>
            </select>
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">Filters (JSON)</label>
            <textarea
              v-model="form.filters"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/50"
              rows="4"
              placeholder='{"status": "active"}'
              data-testid="admin-views-filters-input"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">Columns</label>
            <input
              v-model="form.columns"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
              placeholder="name, status, created_at"
              data-testid="admin-views-columns-input"
            />
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div>
              <label class="mb-1 block text-sm font-medium">Sort By</label>
              <input
                v-model="form.sort_by"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
                placeholder="created_at"
                data-testid="admin-views-sort-by-input"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium">Sort Order</label>
              <select
                v-model="form.sort_order"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
                data-testid="admin-views-sort-order-select"
              >
                <option value="desc">Descending</option>
                <option value="asc">Ascending</option>
              </select>
            </div>
          </div>
          <div v-if="saveError" class="text-sm text-destructive">{{ saveError }}</div>
          <div class="flex items-center gap-2">
            <button
              type="submit"
              :disabled="saving"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground border border-primary/30 hover:border-primary/60 hover:brightness-110 disabled:opacity-50 transition-all"
              data-testid="admin-views-save"
            >
              {{ saving ? 'Saving...' : 'Save' }}
            </button>
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
        <p class="mt-4 text-lg font-medium">No saved views yet</p>
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
              <th class="px-4 py-3 font-medium">Name</th>
              <th class="px-4 py-3 font-medium">Type</th>
              <th class="px-4 py-3 font-medium">Filters</th>
              <th class="px-4 py-3 font-medium">Created By</th>
              <th class="px-4 py-3 font-medium">Created At</th>
              <th class="px-4 py-3 font-medium text-right">Actions</th>
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
              <td class="px-4 py-3 text-muted-foreground max-w-[200px] truncate font-mono text-xs" :title="filtersSummary(v.filters)">{{ filtersSummary(v.filters) }}</td>
              <td class="px-4 py-3 text-muted-foreground">{{ v.created_by || '—' }}</td>
              <td class="px-4 py-3 text-muted-foreground">{{ formatDate(v.created_at) }}</td>
              <td class="px-4 py-3 text-right">
                <div class="flex items-center justify-end gap-1">
                  <button
                    class="rounded p-1 text-muted-foreground hover:bg-accent"
                    data-testid="admin-views-duplicate"
                    :aria-label="'Duplicate view'"
                    title="Duplicate"
                    @click="duplicateView(v)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                    </svg>
                  </button>
                  <button
                    class="rounded p-1 text-muted-foreground hover:bg-accent"
                    data-testid="admin-views-edit"
                    :aria-label="'Edit view'"
                    title="Edit view"
                    @click="openEditForm(v)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                    </svg>
                  </button>
                  <button
                    class="rounded p-1 text-destructive hover:bg-destructive/10"
                    data-testid="admin-views-delete"
                    :aria-label="'Delete view'"
                    title="Delete view"
                    @click="confirmDelete(v)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
                    </svg>
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="deleteConfirmId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
        <p class="text-sm font-medium text-destructive">Delete "{{ deleteConfirmName }}"?</p>
        <p class="mt-1 text-sm text-destructive/80">This action cannot be undone.</p>
        <div class="mt-3 flex items-center gap-2">
          <button
            :disabled="deleting"
            class="rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:brightness-110 disabled:opacity-50 transition-all"
            data-testid="admin-views-delete-confirm"
            @click="deleteView"
          >
            {{ deleting ? 'Deleting...' : 'Delete' }}
          </button>
          <button
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
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getAccessToken } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

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

const views = ref<SavedView[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

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

async function loadViews() {
  loading.value = true
  error.value = null
  try {
    const res = await fetch('/api/v1/views', { headers: getHeaders() })
    if (!res.ok) {
      const errData = await res.json().catch(() => null)
      throw new Error(errData?.detail ?? `Failed to load views (${res.status})`)
    }
    const data = await res.json()
    views.value = data.items ?? data
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function filtersSummary(filters: SavedView['filters']): string {
  if (!filters) return '—'
  const str = typeof filters === 'string' ? filters : JSON.stringify(filters)
  return str.length > 60 ? str.slice(0, 60) + '…' : str
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '—'
  try {
    return new Date(dateStr).toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
    })
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
    saveError.value = e instanceof Error ? e.message : String(e)
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
    views.value = views.value.filter(v => v.id !== deleteConfirmId.value)
    deleteConfirmId.value = null
  } catch (e: unknown) {
    deleteError.value = e instanceof Error ? e.message : String(e)
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
    error.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(loadViews)
</script>
