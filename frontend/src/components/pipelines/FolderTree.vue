<template>
  <div data-testid="folder-tree" class="w-64 border-r border-border h-screen sticky top-0 overflow-y-auto bg-card flex flex-col">
    <div class="p-3 border-b border-border flex items-center justify-between shrink-0">
      <h3 class="text-sm font-semibold text-foreground">{{ $t('views.PipelineListView.folders') }}</h3>
      <button
        data-testid="folder-tree-new"
        class="rounded p-1 hover:bg-accent text-muted-foreground hover:text-foreground transition-colors"
        @click="openCreateDialog"
        :aria-label="$t('views.PipelineListView.new_folder')"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      </button>
    </div>

    <div class="py-1 flex-1 overflow-y-auto">
      <button
        data-testid="folder-tree-all-pipelines"
        class="flex w-full items-center gap-2 px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
        :class="selectedFolderId === null ? 'bg-accent text-accent-foreground font-medium' : 'text-foreground'"
        @click="$emit('select-folder', null)"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        {{ $t('views.PipelineListView.all_pipelines') }}
      </button>

      <div v-if="loading" class="px-3 py-2 space-y-2">
        <div v-for="i in 3" :key="i" class="h-5 w-3/4 bg-muted rounded animate-pulse" />
      </div>

      <div v-else-if="error" class="px-3 py-2 text-sm text-destructive">
        {{ error }}
      </div>

      <div v-else-if="flatTree.length === 0" class="px-3 py-4 text-xs text-muted-foreground text-center">
        No folders yet
      </div>

      <template v-for="item in flatTree" :key="item.folder.id">
        <button
          :data-testid="`folder-tree-item-${item.folder.id}`"
          class="flex w-full items-center gap-2 px-3 py-2 text-sm hover:bg-accent transition-colors group text-left"
          :class="[
            selectedFolderId === item.folder.id ? 'bg-accent text-accent-foreground font-medium' : 'text-foreground',
          ]"
          :style="{ paddingLeft: `${12 + item.depth * 16}px` }"
          @click="$emit('select-folder', item.folder.id)"
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0 text-muted-foreground"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
          <span class="truncate">{{ item.folder.name }}</span>
          <div class="ml-auto flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
            <button
              class="rounded p-0.5 hover:bg-accent-foreground/10 text-muted-foreground hover:text-foreground transition-colors"
              @click.stop="openRenameDialog(item.folder)"
              :aria-label="$t('views.PipelineListView.rename_folder')"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
            </button>
            <button
              class="rounded p-0.5 hover:bg-destructive/10 text-muted-foreground hover:text-destructive transition-colors"
              @click.stop="openDeleteConfirm(item.folder)"
              :aria-label="$t('views.PipelineListView.delete_folder')"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
            </button>
          </div>
        </button>
      </template>
    </div>

    <!-- Create Folder Dialog -->
    <div
      v-if="showCreateDialog"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
    >
      <button
        type="button"
        class="absolute inset-0 cursor-default"
        :aria-label="$t('common.close')"
        @click="showCreateDialog = false"
      ></button>
      <div class="relative w-full max-w-md rounded-lg border bg-card p-6 shadow-lg" role="dialog" aria-modal="true">
        <h3 class="mb-4 text-lg font-semibold">{{ $t('views.PipelineListView.new_folder') }}</h3>
        <div class="space-y-4">
          <div>
            <label for="folder-tree-new-name" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineListView.folder_name') }}</label>
            <input id="folder-tree-new-name"
              v-model="newFolderName"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.PipelineListView.folder_name')"
              @keyup.enter="handleCreate"
            />
          </div>
          <div v-if="createError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ createError }}
          </div>
          <div class="flex justify-end gap-2">
            <button class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent" @click="showCreateDialog = false">
              {{ $t('common.cancel') }}
            </button>
            <Button :disabled="!newFolderName.trim() || creating" @click="handleCreate">
              {{ creating ? $t('common.saving') : $t('common.save') }}
            </Button>
          </div>
        </div>
      </div>
    </div>

    <!-- Rename Folder Dialog -->
    <div
      v-if="showRenameDialog"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
    >
      <button
        type="button"
        class="absolute inset-0 cursor-default"
        :aria-label="$t('common.close')"
        @click="showRenameDialog = false"
      ></button>
      <div class="relative w-full max-w-md rounded-lg border bg-card p-6 shadow-lg" role="dialog" aria-modal="true">
        <h3 class="mb-4 text-lg font-semibold">{{ $t('views.PipelineListView.rename_folder') }}</h3>
        <div class="space-y-4">
          <div>
            <label for="folder-tree-rename-name" class="mb-1 block text-sm font-medium">{{ $t('views.PipelineListView.folder_name') }}</label>
            <input id="folder-tree-rename-name"
              v-model="renameFolderName"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.PipelineListView.folder_name')"
              @keyup.enter="handleRename"
            />
          </div>
          <div v-if="renameError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ renameError }}
          </div>
          <div class="flex justify-end gap-2">
            <button class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent" @click="showRenameDialog = false">
              {{ $t('common.cancel') }}
            </button>
            <Button :disabled="!renameFolderName.trim() || renaming" @click="handleRename">
              {{ renaming ? $t('common.saving') : $t('common.save') }}
            </Button>
          </div>
        </div>
      </div>
    </div>

    <!-- Delete Confirmation Dialog -->
    <div
      v-if="showDeleteConfirm"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
    >
      <button
        type="button"
        class="absolute inset-0 cursor-default"
        :aria-label="$t('common.close')"
        @click="showDeleteConfirm = false"
      ></button>
      <div class="relative w-full max-w-md rounded-lg border bg-card p-6 shadow-lg" role="dialog" aria-modal="true">
        <h3 class="mb-4 text-lg font-semibold text-destructive">{{ $t('views.PipelineListView.delete_folder') }}</h3>
        <p class="mb-4 text-sm text-muted-foreground">
          {{ $t('views.PipelineListView.delete_folder_confirm') }}
        </p>
        <div v-if="deleteError" class="mb-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          {{ deleteError }}
        </div>
        <div class="flex justify-end gap-2">
          <button class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent" @click="showDeleteConfirm = false">
            {{ $t('common.cancel') }}
          </button>
          <button class="rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90" :disabled="deleting" @click="handleDelete">
            {{ $t('common.delete') }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { formatApiError } from '../../lib/api/formatError'
import { Button } from '@/components/ui/button'

interface FolderItem {
  id: string
  organisation_id: string
  name: string
  parent_id: string | null
  sort_order: number
}

interface FlatTreeItem {
  folder: FolderItem
  depth: number
}

const props = defineProps<{
  selectedFolderId: string | null
}>()

const emit = defineEmits<{
  (e: 'select-folder', folderId: string | null): void
  (e: 'folders-changed'): void
}>()

const { get, post, patch, delete: deleteRequest } = useApi()

const allFolders = ref<FolderItem[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const showCreateDialog = ref(false)
const newFolderName = ref('')
const createError = ref<string | null>(null)
const creating = ref(false)

const showRenameDialog = ref(false)
const renameTarget = ref<FolderItem | null>(null)
const renameFolderName = ref('')
const renameError = ref<string | null>(null)
const renaming = ref(false)

const showDeleteConfirm = ref(false)
const deleteTarget = ref<FolderItem | null>(null)
const deleteError = ref<string | null>(null)
const deleting = ref(false)

const folderChildren = computed(() => {
  const children = new Map<string, FolderItem[]>()
  for (const f of allFolders.value) {
    if (f.parent_id) {
      if (!children.has(f.parent_id)) children.set(f.parent_id, [])
      children.get(f.parent_id)!.push(f)
    }
  }
  return children
})

const folderRoots = computed(() =>
  allFolders.value.filter(f => !f.parent_id)
)

const flatTree = computed(() => {
  const result: FlatTreeItem[] = []
  function walk(items: FolderItem[], depth: number) {
    for (const item of items) {
      result.push({ folder: item, depth })
      const kids = folderChildren.value.get(item.id)
      if (kids) walk(kids, depth + 1)
    }
  }
  walk(folderRoots.value, 0)
  return result
})

async function loadFolders() {
  loading.value = true
  error.value = null
  try {
    allFolders.value = await get<FolderItem[]>('/api/v1/pipeline-folders')
  } catch (e: unknown) {
    error.value = formatApiError(e)
  } finally {
    loading.value = false
  }
}

function openCreateDialog() {
  newFolderName.value = ''
  createError.value = null
  showCreateDialog.value = true
}

async function handleCreate() {
  if (!newFolderName.value.trim()) return
  creating.value = true
  createError.value = null
  try {
    await post<FolderItem>('/api/v1/pipeline-folders', { name: newFolderName.value.trim() })
    showCreateDialog.value = false
    emit('folders-changed')
    await loadFolders()
  } catch (e: unknown) {
    createError.value = formatApiError(e)
  } finally {
    creating.value = false
  }
}

function openRenameDialog(folder: FolderItem) {
  renameTarget.value = folder
  renameFolderName.value = folder.name
  renameError.value = null
  showRenameDialog.value = true
}

async function handleRename() {
  if (!renameTarget.value || !renameFolderName.value.trim()) return
  renaming.value = true
  renameError.value = null
  try {
    await patch<FolderItem>(`/api/v1/pipeline-folders/${renameTarget.value.id}`, {
      name: renameFolderName.value.trim(),
    })
    showRenameDialog.value = false
    renameTarget.value = null
    emit('folders-changed')
    await loadFolders()
  } catch (e: unknown) {
    renameError.value = formatApiError(e)
  } finally {
    renaming.value = false
  }
}

function openDeleteConfirm(folder: FolderItem) {
  deleteTarget.value = folder
  deleteError.value = null
  showDeleteConfirm.value = true
}

async function handleDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  deleteError.value = null
  try {
    await deleteRequest<void>(`/api/v1/pipeline-folders/${deleteTarget.value.id}`)
    const deletedId = deleteTarget.value.id
    showDeleteConfirm.value = false
    deleteTarget.value = null

    if (props.selectedFolderId === deletedId) {
      emit('select-folder', null)
    }

    emit('folders-changed')
    await loadFolders()
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  loadFolders()
})
</script>
