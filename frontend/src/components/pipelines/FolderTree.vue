<template>
  <div data-testid="folder-tree" class="hidden md:flex w-64 border-r border-border h-screen sticky top-0 overflow-y-auto bg-card flex-col">
    <div class="p-3 border-b border-border flex items-center justify-between shrink-0">
      <h3 class="text-sm font-semibold text-foreground">{{ labels.folders }}</h3>
      <button type="button"
        data-testid="folder-tree-new"
        class="rounded p-1 hover:bg-accent text-muted-foreground hover:text-foreground transition-colors"
        @click="openCreateDialog"
        :aria-label="labels.newFolder"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      </button>
    </div>

    <div class="py-1 flex-1 overflow-y-auto">
      <button type="button"
        data-testid="folder-tree-all-pipelines"
        class="flex w-full items-center gap-2 px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
        :class="[
          selectedFolderId === null ? 'bg-accent text-accent-foreground font-medium' : 'text-foreground',
          dragOverRoot ? 'bg-accent/70 ring-1 ring-inset ring-primary' : '',
        ]"
        @click="$emit('select-folder', null)"
        @dragover.prevent="dragOverRoot = true"
        @dragleave="dragOverRoot = false"
        @drop="onRootDrop($event)"
      >
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <span class="truncate">{{ labels.allItems }}</span>
        <span v-if="counts?.['__all__'] !== undefined" class="ml-auto text-xs text-muted-foreground">{{ counts['__all__'] }}</span>
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

      <draggable
        v-model="draggableItems"
        :item-key="(item: FlatTreeItem) => item.folder.id"
        ghost-class="opacity-40"
        :animation="200"
        handle=".drag-handle"
        @end="onDragEnd"
        @change="onDragChange"
      >
        <template #item="{ element }">
          <div
            :data-testid="`folder-tree-item-${element.folder.id}`"
            :class="[
              'group flex w-full items-center gap-2 px-3 py-2 text-sm hover:bg-accent transition-colors text-left',
              selectedFolderId === element.folder.id ? 'bg-accent text-accent-foreground font-medium' : 'text-foreground',
            ]"
            :style="{ paddingLeft: `${12 + element.depth * 16}px` }"
            role="button"
            tabindex="0"
            @click="$emit('select-folder', element.folder.id)"
            @keydown.enter="$emit('select-folder', element.folder.id)"
            @keydown.space.prevent="$emit('select-folder', element.folder.id)"
            @dragover.prevent
            @drop="onFolderDrop(element.folder.id, $event)"
          >
            <!-- Drag handle -->
            <span class="drag-handle cursor-grab active:cursor-grabbing opacity-0 group-hover:opacity-100 transition-opacity shrink-0 text-muted-foreground hover:text-foreground">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="5" r="1"/><circle cx="15" cy="5" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="9" cy="19" r="1"/><circle cx="15" cy="19" r="1"/></svg>
            </span>

            <!-- Folder icon -->
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0 text-muted-foreground"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>

            <!-- Folder name -->
            <span class="truncate">{{ element.folder.name }}</span>
            <span v-if="counts?.[element.folder.id] !== undefined" class="ml-1 text-xs text-muted-foreground shrink-0">{{ counts[element.folder.id] }}</span>

            <!-- Action buttons (rename, delete) -->
            <div class="ml-auto flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
              <button type="button"
                class="rounded p-0.5 hover:bg-accent-foreground/10 text-muted-foreground hover:text-foreground transition-colors"
                @click.stop="openRenameDialog(element.folder)"
                :aria-label="labels.renameFolder"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>
              </button>
              <button type="button"
                class="rounded p-0.5 hover:bg-destructive/10 text-muted-foreground hover:text-destructive transition-colors"
                @click.stop="openDeleteConfirm(element.folder)"
                :aria-label="labels.deleteFolder"
              >
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
              </button>
            </div>
          </div>
        </template>
      </draggable>
    </div>

    <!-- Create Folder Dialog -->
    <Dialog v-model:visible="showCreateDialog" :modal="true" :dismissable-mask="true" :style="{ width: '28rem' }">
      <template #header>
        <div class="text-lg font-semibold">{{ labels.newFolder }}</div>
      </template>
      <div class="space-y-4">
        <div>
          <label for="folder-tree-new-name" class="mb-1 block text-sm font-medium">{{ labels.folderName }}</label>
          <InputText id="folder-tree-new-name" v-model="newFolderName" placeholder="Folder name" class="w-full" @keyup.enter="handleCreate" />
        </div>

        <!-- Parent folder selector -->
        <div>
          <label for="folder-tree-parent" class="mb-1 block text-sm font-medium">{{ $t('components.pipelines.FolderTree.parent_folder') }}</label>
          <Select
  :aria-label="$t('components.pipelines.FolderTree.parent_folder')"
  v-model="newFolderParentId"
  :placeholder="$t('components.pipelines.FolderTree.no_parent_root_level')"
  id="folder-tree-parent"
  class="w-full"
  :options="[{ value: 'null', label: $t('components.pipelines.FolderTree.no_parent_root_level') }, ...allFolders.map(f => ({ value: f.id, label: f.name }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>

        <div v-if="createError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          {{ createError }}
        </div>
      </div>
      <template #footer>
        <div class="flex gap-2 justify-end">
          <Button severity="secondary" outlined @click="showCreateDialog = false">{{ $t('common.cancel') }}</Button>
          <Button :disabled="!newFolderName.trim() || creating" :loading="creating" @click="handleCreate">{{ $t('common.save') }}</Button>
        </div>
      </template>
    </Dialog>

    <!-- Rename Folder Dialog -->
    <Dialog v-model:visible="showRenameDialog" :modal="true" :dismissable-mask="true" :style="{ width: '28rem' }">
      <template #header>
        <div class="text-lg font-semibold">{{ labels.renameFolder }}</div>
      </template>
      <div class="space-y-4">
        <div>
          <label for="folder-tree-rename-name" class="mb-1 block text-sm font-medium">{{ labels.folderName }}</label>
          <InputText id="folder-tree-rename-name" v-model="renameFolderName" placeholder="Folder name" class="w-full" @keyup.enter="handleRename" />
        </div>
        <div v-if="renameError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          {{ renameError }}
        </div>
      </div>
      <template #footer>
        <div class="flex gap-2 justify-end">
          <Button severity="secondary" outlined @click="showRenameDialog = false">{{ $t('common.cancel') }}</Button>
          <Button :disabled="!renameFolderName.trim() || renaming" :loading="renaming" @click="handleRename">{{ $t('common.save') }}</Button>
        </div>
      </template>
    </Dialog>

    <!-- Delete Confirmation Dialog -->
    <Dialog v-model:visible="showDeleteConfirm" :modal="true" :dismissable-mask="true" :style="{ width: '28rem' }">
      <template #header>
        <div class="text-lg font-semibold text-destructive">{{ labels.deleteFolder }}</div>
      </template>
      <p class="text-sm text-muted-foreground">{{ deleteConfirmMessage }}</p>
      <div v-if="deleteError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
        {{ deleteError }}
      </div>
      <template #footer>
        <div class="flex gap-2 justify-end">
          <Button severity="secondary" outlined @click="showDeleteConfirm = false">{{ $t('common.cancel') }}</Button>
          <Button severity="danger" :disabled="deleting" :loading="deleting" @click="handleDelete">{{ $t('common.delete') }}</Button>
        </div>
      </template>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import draggable from 'vuedraggable'
import { useApi } from '@/composables/useApi'
import { formatApiError } from '../../lib/api/formatError'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'

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
  /** @deprecated use itemCounts */
  pipelineCounts?: Record<string, number>
  itemCounts?: Record<string, number>
  /** API prefix for the folder resource, e.g. /api/v1/pipeline-folders */
  apiBase?: string
  /** i18n namespace holding the folder labels (folders, new_folder, ...) */
  i18nNs?: string
  /** i18n key (within i18nNs) for the "all items" entry */
  allItemsKey?: string
  /** noun used in the delete-confirmation copy, e.g. "Pipelines" */
  itemNoun?: string
}>()

const emit = defineEmits<{
  (e: 'select-folder', folderId: string | null): void
  (e: 'folders-changed'): void
  (e: 'move-pipeline', payload: { pipelineId: string; folderId: string | null }): void
}>()

const { t } = useI18n()
const { get, post, patch, delete: deleteRequest } = useApi()

const apiBase = computed(() => props.apiBase ?? '/api/v1/pipeline-folders')
const counts = computed(() => props.itemCounts ?? props.pipelineCounts)
const itemNoun = computed(() => props.itemNoun ?? 'Pipelines')

const labels = computed(() => {
  const ns = props.i18nNs ?? 'views.PipelineListView'
  return {
    folders: t(`${ns}.folders`),
    newFolder: t(`${ns}.new_folder`),
    allItems: t(`${ns}.${props.allItemsKey ?? 'all_pipelines'}`),
    renameFolder: t(`${ns}.rename_folder`),
    deleteFolder: t(`${ns}.delete_folder`),
    folderName: t(`${ns}.folder_name`),
    uncategorised: t(`${ns}.uncategorised`),
  }
})

const allFolders = ref<FolderItem[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const draggableItems = ref<FlatTreeItem[]>([])
const dragOverRoot = ref(false)
const showCreateDialog = ref(false)
const newFolderName = ref('')
const newFolderParentId = ref<string | null>(null)
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

const deleteConfirmMessage = computed(() => {
  if (!deleteTarget.value) return ''
  const count = counts.value?.[deleteTarget.value.id]
  if (count && count > 0) {
    return t('components.pipelines.FolderTree.delete_confirm_with_items', {
      itemNoun: itemNoun.value,
      uncategorisedLabel: labels.value.uncategorised,
    })
  }
  return t('components.pipelines.FolderTree.delete_confirm_empty')
})

async function loadFolders() {
  loading.value = true
  error.value = null
  try {
    allFolders.value = await get<FolderItem[]>(apiBase.value)
  } catch (e: unknown) {
    error.value = formatApiError(e)
  } finally {
    loading.value = false
  }
}

watch(allFolders, () => {
  draggableItems.value = [...flatTree.value]
}, { immediate: true })

function onFolderDrop(folderId: string, event: DragEvent) {
  const pipelineId = event.dataTransfer?.getData('text/plain')
  if (pipelineId) {
    emit('move-pipeline', { pipelineId, folderId })
    emit('folders-changed')
  }
}

function onRootDrop(event: DragEvent) {
  dragOverRoot.value = false
  const itemId = event.dataTransfer?.getData('text/plain')
  if (itemId) {
    emit('move-pipeline', { pipelineId: itemId, folderId: null })
    emit('folders-changed')
  }
}

async function onDragEnd() {
  try {
    const itemsByDepth: Record<number, FlatTreeItem[]> = {}
    for (const item of draggableItems.value) {
      if (!itemsByDepth[item.depth]) itemsByDepth[item.depth] = []
      itemsByDepth[item.depth].push(item)
    }

    for (const depthStr of Object.keys(itemsByDepth)) {
      const items = itemsByDepth[Number(depthStr)]
      for (let i = 0; i < items.length; i++) {
        const folder = items[i].folder
        if (folder.sort_order !== i) {
          try {
            await patch(`${apiBase.value}/${folder.id}/move`, { sort_order: i })
          } catch {
            console.warn(`Failed to update sort_order for folder ${folder.id}`)
          }
        }
      }
    }
  } catch {
    await loadFolders()
  }
}

function onDragChange() {
  // Visual feedback only — persistence handled in onDragEnd
}

function openCreateDialog() {
  newFolderName.value = ''
  newFolderParentId.value = null
  createError.value = null
  showCreateDialog.value = true
}

async function handleCreate() {
  if (!newFolderName.value.trim()) return
  creating.value = true
  createError.value = null
  try {
    const body: Record<string, any> = { name: newFolderName.value.trim() }
    if (newFolderParentId.value && newFolderParentId.value !== 'null') {
      body.parent_id = newFolderParentId.value
    }
    await post<FolderItem>(apiBase.value, body)
    showCreateDialog.value = false
    newFolderParentId.value = null
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
    await patch<FolderItem>(`${apiBase.value}/${renameTarget.value.id}`, {
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
    await deleteRequest<void>(`${apiBase.value}/${deleteTarget.value.id}`)
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
