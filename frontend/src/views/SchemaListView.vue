<template>
  <PageTabs :tabs="[
    { label: $t('views.SchemaInferenceView.browse'), to: '/schemas' },
    { label: $t('views.SchemaInferenceView.editor'), to: '/schemas/editor' },
    { label: $t('views.SchemaInferenceView.infer'), to: '/schemas/infer' },
  ]" />
    <div class="page-wide">
    <PageHeader :title="$t('views.SchemaListView.schemas')" :subtitle="$t('views.SchemaListView.manage_schemas_and_deprecate_outdated_definitions')" />

    <div class="flex">
      <!-- Folder sidebar -->
      <FolderTree
        :selected-folder-id="selectedFolderId"
        :item-counts="folderSchemaCounts"
        api-base="/api/v1/schema-folders"
        i18n-ns="views.SchemaListView"
        all-items-key="all_schemas"
        item-noun="Schemas"
        @select-folder="onSelectFolder"
        @folders-changed="onFoldersChanged"
        @move-pipeline="onMoveSchema"
      />

      <div class="flex-1 min-w-0">
        <div v-if="folderError" class="mb-4 px-4 py-2 text-xs text-destructive">
          {{ $t('views.SchemaListView.failed_to_load_schemas') }} {{ folderError }}
        </div>

        <div v-if="moveError" class="mb-4 flex items-center justify-between gap-3 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive" role="alert" data-testid="schema-list-move-error">
          <span>{{ moveError }}</span>
          <button class="shrink-0 text-destructive/70 hover:text-destructive" :aria-label="$t('views.SchemaListView.cancel')" @click="moveError = null">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>

        <div v-if="loading" aria-hidden="true" class="overflow-x-auto rounded-lg border">
          <table class="w-full text-left text-sm">
            <thead class="bg-muted/50">
              <tr>
                <th class="px-4 py-3 font-medium">{{ $t('views.SchemaListView.name') }}</th>
                <th class="px-4 py-3 font-medium">{{ $t('views.SchemaListView.description') }}</th>
                <th class="px-4 py-3 font-medium capitalize">{{ $t('views.SchemaListView.status') }}</th>
                <th class="px-4 py-3 font-medium text-right">{{ $t('views.SchemaListView.actions') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr v-for="row in 6" :key="row">
                <td class="px-4 py-3"><div class="h-4 w-32 rounded bg-muted/50" /></td>
                <td class="px-4 py-3"><div class="h-4 w-full max-w-md rounded bg-muted/50" /></td>
                <td class="px-4 py-3"><div class="h-4 w-16 rounded bg-muted/50" /></td>
                <td class="px-4 py-3"><div class="ml-auto h-4 w-8 rounded bg-muted/50" /></td>
              </tr>
            </tbody>
          </table>
        </div>

        <ErrorAlert v-else-if="error" :message="error" />

        <template v-else>
          <!-- Mobile folder filter — the FolderTree is hidden below md -->
          <div v-if="foldersList.length > 0" class="md:hidden mb-4">
            <Select v-model="mobileFolderSelectValue" :aria-label="$t('views.SchemaListView.folders')">
              <SelectTrigger class="w-full" :aria-label="$t('views.SchemaListView.folders')" data-testid="schema-list-mobile-folder-select">
                <SelectValue :placeholder="$t('views.SchemaListView.all_schemas')" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__all__">{{ $t('views.SchemaListView.all_schemas') }}</SelectItem>
                <SelectItem v-for="f in foldersList" :key="f.id" :value="f.id">{{ f.name }}</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <!-- Breadcrumb navigation -->
          <div class="mb-4 flex items-center gap-2 text-sm">
            <template v-if="selectedFolderId && selectedFolderName">
              <button class="text-muted-foreground hover:text-foreground transition-colors" @click="onSelectFolder(null)">
                {{ $t('views.SchemaListView.all_schemas') }}
              </button>
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="text-muted-foreground"><polyline points="9 18 15 12 9 6"/></svg>
              <span class="font-medium text-foreground">{{ selectedFolderName }}</span>
            </template>
            <h2 v-else class="text-base font-semibold text-foreground">{{ $t('views.SchemaListView.all_schemas') }}</h2>
          </div>

          <div v-if="schemas.length === 0" class="card p-8 text-center">
            <p class="text-lg font-medium">{{ $t('views.SchemaListView.no_schemas_found') }}</p>
            <p class="mt-1 text-sm text-muted-foreground">
              {{ $t('views.SchemaListView.empty_hint') }}
            </p>
          </div>

          <template v-else>
            <div class="overflow-x-auto rounded-lg border">
              <table class="w-full text-left text-sm">
                <thead class="bg-muted/50">
                  <tr>
                    <th class="px-4 py-3 font-medium">{{ $t('views.SchemaListView.name') }}</th>
                    <th class="px-4 py-3 font-medium">{{ $t('views.SchemaListView.description') }}</th>
                    <th class="px-4 py-3 font-medium capitalize">{{ $t('views.SchemaListView.status') }}</th>
                    <th class="px-4 py-3 font-medium text-right">{{ $t('views.SchemaListView.actions') }}</th>
                  </tr>
                </thead>
                <tbody class="divide-y">
                  <tr
                    v-for="schema in schemas"
                    :key="schema.id"
                    class="cursor-pointer hover:bg-muted/30 transition-colors"
                    :data-testid="`schema-row-${schema.id}`"
                    @click="openEditor(schema)"
                    draggable="true"
                    @dragstart="onSchemaDragStart(schema, $event)"
                    @dragover.prevent
                  >
                    <td class="px-4 py-3 font-medium">{{ schema.name }}</td>
                    <td class="px-4 py-3 text-muted-foreground">{{ schema.description || '—' }}</td>
                    <td class="px-4 py-3">
                      <span
                        v-if="schema.deprecated"
                        class="inline-flex items-center gap-1 rounded-full bg-destructive/10 px-2.5 py-0.5 text-xs font-medium text-destructive"
                      >
                        <span class="h-1.5 w-1.5 rounded-full bg-destructive" />
                        {{ $t('views.SchemaListView.deprecated') }}
                      </span>
                      <span
                        v-else
                        class="inline-flex items-center gap-1 rounded-full bg-success/10 px-2.5 py-0.5 text-xs font-medium text-success"
                      >
                        <span class="h-1.5 w-1.5 rounded-full bg-success" />
                        {{ $t('views.SchemaListView.active') }}
                      </span>
                    </td>
                    <td class="px-4 py-3 text-right">
                      <DropdownMenu>
                        <DropdownMenuTrigger as-child>
                          <button
                            class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                            data-testid="schema-action-menu"
                            :aria-label="$t('views.SchemaListView.schema_actions')"
                            @click.stop
                          >
                            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/></svg>
                          </button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" class="w-40">
                          <DropdownMenuItem data-testid="schema-view-edit" @click.stop="openEditor(schema)">
                            {{ $t('views.SchemaListView.view_edit') }}
                          </DropdownMenuItem>
                          <DropdownMenuItem data-testid="schema-move-folder" @click.stop="openMoveToFolder(schema)">
                            {{ $t('views.SchemaListView.move_to_folder') }}
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            v-if="!schema.deprecated"
                            class="text-destructive focus:text-destructive"
                            data-testid="schema-deprecate"
                            @click.stop="confirmDeprecate(schema)"
                          >
                            {{ $t('views.SchemaListView.deprecate') }}
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </template>
        </template>
      </div>
    </div>

    <!-- Deprecation Confirmation Dialog -->
    <Dialog v-model:open="deprecateDialogOpen">
      <DialogContent data-testid="schema-deprecate-dialog">
        <DialogHeader>
          <DialogTitle>{{ $t('views.SchemaListView.deprecation_title', { name: deprecateConfirmName }) }}</DialogTitle>
          <DialogDescription>{{ $t('views.SchemaListView.deprecation_description') }}</DialogDescription>
        </DialogHeader>
        <div v-if="deprecateError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          {{ deprecateError }}
        </div>
        <DialogFooter>
          <Button variant="outline" data-testid="schema-deprecate-cancel" @click="deprecateDialogOpen = false">
            {{ $t('views.SchemaListView.cancel') }}
          </Button>
          <Button variant="destructive" data-testid="schema-deprecate-confirm" :disabled="deprecating" :loading="deprecating" @click="deprecateSchema">
            {{ deprecating ? $t('views.SchemaListView.deprecating') : $t('views.SchemaListView.deprecate') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <!-- Move to Folder Dialog -->
    <Dialog v-model:open="showMoveToFolder">
      <DialogContent data-testid="schema-move-dialog">
        <DialogHeader>
          <DialogTitle>{{ $t('views.SchemaListView.move_to_folder') }}</DialogTitle>
          <DialogDescription v-if="moveTarget">
            {{ $t('views.SchemaListView.move_to_folder_description', { name: moveTarget.name }) }}
          </DialogDescription>
        </DialogHeader>
        <div class="space-y-2">
          <button
            v-for="f in foldersList"
            :key="f.id"
            class="flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
            :class="moveToFolderId === f.id ? 'border-primary bg-accent' : 'border-border'"
            :data-testid="`schema-move-folder-${f.id}`"
            @click="moveToFolderId = f.id"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0 text-muted-foreground"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
            {{ f.name }}
          </button>
          <button
            class="flex w-full items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
            :class="moveToFolderId === null ? 'border-primary bg-accent' : ''"
            data-testid="schema-move-nofolder"
            @click="moveToFolderId = null"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0 text-muted-foreground"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
            {{ $t('views.SchemaListView.no_folder') }}
          </button>
          <div v-if="moveError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive" role="alert">
            {{ moveError }}
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" data-testid="schema-move-cancel" @click="showMoveToFolder = false">
            {{ $t('common.cancel') }}
          </Button>
          <Button data-testid="schema-move-confirm" :disabled="moving" :loading="moving" @click="handleMoveToFolder">
            {{ moving ? $t('common.saving') : $t('common.save') }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { useApi } from '../composables/useApi'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import FolderTree from '../components/pipelines/FolderTree.vue'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import PageTabs from "../components/PageTabs.vue"

type SchemaItem = components['schemas']['modulo__api__routes__schemas__SchemaResponse'] & {
  folder_id?: string | null
}

interface SchemaListResponse {
  items: SchemaItem[]
  total: number
  page: number
  page_size: number
}

interface FolderItem {
  id: string
  organisation_id: string
  name: string
  parent_id: string | null
  sort_order: number
}

const router = useRouter()
const { get, patch: patchUntyped } = useApi()

const selectedFolderId = ref<string | null>(null)

const { loading, error, data: schemasResp, load: loadSchemas } = useDataFetch<SchemaListResponse>(
  () => {
    const query: { page: number; page_size: number; folder_id?: string } = { page: 1, page_size: 100 }
    if (selectedFolderId.value) {
      query.folder_id = selectedFolderId.value
    }
    return api.GET('/api/v1/schemas', {
      params: { query },
    })
  },
  { initialValue: { items: [] as SchemaItem[], total: 0, page: 1, page_size: 100 } },
)

const schemas = computed(() => schemasResp.value?.items ?? [])

const foldersList = ref<FolderItem[]>([])
const folderError = ref<string | null>(null)

const folderSchemaCounts = computed(() => {
  const counts: Record<string, number> = {}
  for (const s of allSchemasForCounts.value) {
    if (s.folder_id) {
      counts[s.folder_id] = (counts[s.folder_id] || 0) + 1
    }
  }
  counts.__all__ = allSchemasForCounts.value.length
  return counts
})

const allSchemasForCounts = ref<SchemaItem[]>([])

async function loadAllSchemasForCounts() {
  try {
    const items: SchemaItem[] = []
    let page = 1
    let total = 0
    do {
      const { data } = await api.GET('/api/v1/schemas', {
        params: { query: { page, page_size: 100 } },
      })
      if (!data) break
      total = data.total
      items.push(...data.items)
      if (data.items.length === 0) break
      page += 1
    } while ((page - 1) * 100 < total)
    allSchemasForCounts.value = items
  } catch (e: unknown) {
    console.warn('Failed to load schema counts', e)
  }
}

const folderNameMap = computed(() => {
  const map = new Map<string, string>()
  for (const f of foldersList.value) {
    map.set(f.id, f.name)
  }
  return map
})

const selectedFolderName = computed(() => {
  if (!selectedFolderId.value) return ''
  return folderNameMap.value.get(selectedFolderId.value) || ''
})

const mobileFolderSelectValue = computed<string>({
  get: () => selectedFolderId.value ?? '__all__',
  set: (val: string) => onSelectFolder(val === '__all__' ? null : val),
})

async function loadFolders() {
  folderError.value = null
  try {
    foldersList.value = await get<FolderItem[]>('/api/v1/schema-folders')
  } catch (e: unknown) {
    folderError.value = formatApiError(e)
  }
}

function onSelectFolder(folderId: string | null) {
  selectedFolderId.value = folderId
  loadSchemas()
}

function onFoldersChanged() {
  loadSchemas()
  loadFolders()
  loadAllSchemasForCounts()
}

function onSchemaDragStart(schema: SchemaItem, event: DragEvent) {
  event.dataTransfer?.setData('text/plain', schema.id)
  event.dataTransfer!.effectAllowed = 'move'
}

const moving = ref(false)
const moveError = ref<string | null>(null)

async function moveSchema(schemaId: string, folderId: string | null) {
  if (moving.value) return
  const schema = schemas.value.find(s => s.id === schemaId)
  if (!schema) return
  if ((schema.folder_id ?? null) === folderId) return
  moveError.value = null
  moving.value = true
  try {
    await patchUntyped(`/api/v1/schemas/${schemaId}/folder`, {
      folder_id: folderId,
    })
    await loadSchemas()
    await loadAllSchemasForCounts()
  } catch (e: unknown) {
    moveError.value = formatApiError(e)
  } finally {
    moving.value = false
  }
}

async function onMoveSchema(ev: { pipelineId: string; folderId: string | null }) {
  await moveSchema(ev.pipelineId, ev.folderId)
}

const showMoveToFolder = ref(false)
const moveTarget = ref<SchemaItem | null>(null)
const moveToFolderId = ref<string | null>(null)

function openMoveToFolder(schema: SchemaItem) {
  moveTarget.value = schema
  moveToFolderId.value = schema.folder_id ?? null
  moveError.value = null
  showMoveToFolder.value = true
}

async function handleMoveToFolder() {
  if (!moveTarget.value) return
  const targetId = moveTarget.value.id
  const folderId = moveToFolderId.value
  await moveSchema(targetId, folderId)
  if (!moveError.value) {
    showMoveToFolder.value = false
    moveTarget.value = null
  }
}

function openEditor(schema: SchemaItem) {
  router.push({ name: 'schema-editor', params: { id: schema.id } })
}

const deprecateDialogOpen = ref(false)
const deprecateConfirmId = ref<string | null>(null)
const deprecateConfirmName = ref('')
const deprecating = ref(false)
const deprecateError = ref<string | null>(null)

function confirmDeprecate(schema: SchemaItem) {
  deprecateConfirmId.value = schema.id
  deprecateConfirmName.value = schema.name
  deprecateError.value = null
  deprecateDialogOpen.value = true
}

async function deprecateSchema() {
  if (!deprecateConfirmId.value) return
  deprecating.value = true
  deprecateError.value = null
  try {
    const { data, error: err } = await api.PATCH('/api/v1/schemas/{schema_id}/deprecate', {
      params: { path: { schema_id: deprecateConfirmId.value } },
    })
    if (err) {
      deprecateError.value = formatApiError(err)
    } else if (data) {
      const idx = schemas.value.findIndex(s => s.id === deprecateConfirmId.value)
      if (idx >= 0) {
        const s = schemasResp.value
        if (s) {
          schemasResp.value = {
            ...s,
            items: s.items.map((item, itemIdx) => itemIdx === idx ? (data as SchemaItem) : item),
          }
        }
      }
      deprecateDialogOpen.value = false
    }
  } catch (e: unknown) {
    deprecateError.value = formatApiError(e)
  } finally {
    deprecating.value = false
  }
}

onMounted(() => {
  loadFolders()
  loadAllSchemasForCounts()
})
</script>
