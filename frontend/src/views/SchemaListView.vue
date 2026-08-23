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
        :item-noun="$t('views.SchemaListView.schemas')"
        @select-folder="onSelectFolder"
        @folders-changed="onFoldersChanged"
        @move-pipeline="onMoveSchema"
      />

      <div class="flex-1 min-w-0">
        <div v-if="folderError" class="mb-4 px-4 py-2 text-xs text-destructive">
          {{ $t('views.SchemaListView.failed_to_load_folders') }} {{ folderError }}
        </div>

        <div v-if="moveError" class="mb-4 flex items-center justify-between gap-3 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive" role="alert" data-testid="schema-list-move-error">
          <span>{{ moveError }}</span>
          <button type="button" class="shrink-0 text-destructive/70 hover:text-destructive" :aria-label="$t('views.SchemaListView.cancel')" data-testid="schema-list-dismiss-move-error" @click="moveError = null">
            <X class="h-4 w-4" aria-hidden="true" />
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
            <Select
  v-model="mobileFolderSelectValue"
  :aria-label="$t('views.SchemaListView.folders')"
  :placeholder="$t('views.SchemaListView.folders')"
  data-testid="schema-list-mobile-folder-select"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.SchemaListView.all_schemas') }, ...foldersList.map(f => ({ value: f.id, label: f.name }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>

          <!-- Breadcrumb navigation -->
          <div class="mb-4 flex items-center gap-2 text-sm">
            <template v-if="selectedFolderId && selectedFolderName">
              <button type="button" class="text-muted-foreground hover:text-foreground transition-colors" data-testid="schema-list-all-schemas" @click="onSelectFolder(null)">
                {{ $t('views.SchemaListView.all_schemas') }}
              </button>
              <ChevronRight class="h-3 w-3 text-muted-foreground" aria-hidden="true" />
              <span class="font-medium text-foreground">{{ selectedFolderName }}</span>
            </template>
            <h2 v-else class="text-base font-semibold text-foreground">{{ $t('views.SchemaListView.all_schemas') }}</h2>
          </div>

          <EmptyState
            v-if="schemas.length === 0"
            :title="$t('views.SchemaListView.no_schemas_found')"
            :description="$t('views.SchemaListView.empty_hint')"
          />

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
                        <span class="h-1.5 w-1.5 rounded-full bg-destructive" aria-hidden="true" />
                        {{ $t('views.SchemaListView.deprecated') }}
                      </span>
                      <span
                        v-else
                        class="inline-flex items-center gap-1 rounded-full bg-success/10 px-2.5 py-0.5 text-xs font-medium text-success"
                      >
                        <span class="h-1.5 w-1.5 rounded-full bg-success" aria-hidden="true" />
                        {{ $t('views.SchemaListView.active') }}
                      </span>
                    </td>
                    <td class="px-4 py-3 text-right">
                      <button
                        type="button"
                        class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                        data-testid="schema-action-menu"
                        :aria-label="$t('views.SchemaListView.schema_actions')"
                        @click.stop="openActionMenu($event, schema)"
                      >
                        <MoreVertical class="h-3.5 w-3.5" aria-hidden="true" />
                      </button>
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
    <Dialog v-model:visible="deprecateDialogOpen" :modal="true" :dismissable-mask="true" :style="{ width: '28rem' }" data-testid="schema-deprecate-dialog">
      <template #header>
        <div>
          <div class="text-lg font-semibold">{{ $t('views.SchemaListView.deprecation_title', { name: deprecateConfirmName }) }}</div>
          <div class="mt-0.5 text-sm text-muted-foreground">{{ $t('views.SchemaListView.deprecation_description') }}</div>
        </div>
      </template>
      <div v-if="deprecateError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
        {{ deprecateError }}
      </div>
      <template #footer>
        <div class="flex gap-2 justify-end">
          <Button severity="secondary" outlined data-testid="schema-deprecate-cancel" @click="deprecateDialogOpen = false">
            {{ $t('views.SchemaListView.cancel') }}
          </Button>
          <Button severity="danger" data-testid="schema-deprecate-confirm" :disabled="deprecating" :loading="deprecating" @click="deprecateSchema">
            {{ deprecating ? $t('views.SchemaListView.deprecating') : $t('views.SchemaListView.deprecate') }}
          </Button>
        </div>
      </template>
    </Dialog>

    <!-- Move to Folder Dialog -->
    <Dialog v-model:visible="showMoveToFolder" :modal="true" :dismissable-mask="true" :style="{ width: '28rem' }" data-testid="schema-move-dialog">
      <template #header>
        <div>
          <div class="text-lg font-semibold">{{ $t('views.SchemaListView.move_to_folder') }}</div>
          <div v-if="moveTarget" class="mt-0.5 text-sm text-muted-foreground">
            {{ $t('views.SchemaListView.move_to_folder_description', { name: moveTarget.name }) }}
          </div>
        </div>
      </template>
      <div class="space-y-2">
        <button
          type="button"
          v-for="f in foldersList"
          :key="f.id"
          class="flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
          :class="moveToFolderId === f.id ? 'border-primary bg-accent' : 'border-border'"
          :data-testid="`schema-move-folder-${f.id}`"
          @click="moveToFolderId = f.id"
        >
          <Folder class="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          {{ f.name }}
        </button>
        <button
          type="button"
          class="flex w-full items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
          :class="moveToFolderId === null ? 'border-primary bg-accent' : ''"
          data-testid="schema-move-nofolder"
          @click="moveToFolderId = null"
        >
          <FolderOpen class="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          {{ $t('views.SchemaListView.no_folder') }}
        </button>
        <div v-if="moveError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive" role="alert">
          {{ moveError }}
        </div>
      </div>
      <template #footer>
        <div class="flex gap-2 justify-end">
          <Button severity="secondary" outlined data-testid="schema-move-cancel" @click="showMoveToFolder = false">
            {{ $t('common.cancel') }}
          </Button>
          <Button data-testid="schema-move-confirm" :disabled="moving" :loading="moving" @click="handleMoveToFolder">
            {{ moving ? $t('common.saving') : $t('common.save') }}
          </Button>
        </div>
      </template>
    </Dialog>

    <Menu ref="actionMenuRef" :model="actionMenuItems" popup />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { useApi } from '../composables/useApi'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/client'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import FolderTree from '../components/pipelines/FolderTree.vue'
import { ChevronRight, Folder, FolderOpen, MoreVertical, X } from '@lucide/vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Menu from 'primevue/menu'
import Select from 'primevue/select'
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
const { t } = useI18n()
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

const folderSchemaCounts = ref<Record<string, number>>({})

async function loadSchemaCounts() {
  try {
    const { data } = await api.GET('/api/v1/schemas/counts')
    if (!data) return
    folderSchemaCounts.value = { ...(data.by_folder ?? {}), __all__: data.total }
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
  loadSchemaCounts()
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
    await loadSchemaCounts()
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

const actionMenuRef = ref<InstanceType<typeof Menu> | null>(null)
const actionMenuSchema = ref<SchemaItem | null>(null)
const actionMenuItems = computed(() => [
  {
    label: t('views.SchemaListView.view_edit'),
    dataTestid: 'schema-view-edit',
    command: () => {
      if (actionMenuSchema.value) openEditor(actionMenuSchema.value)
    },
  },
  {
    label: t('views.SchemaListView.move_to_folder'),
    dataTestid: 'schema-move-folder',
    command: () => {
      if (actionMenuSchema.value) openMoveToFolder(actionMenuSchema.value)
    },
  },
  ...(!actionMenuSchema.value?.deprecated
    ? [{
        label: t('views.SchemaListView.deprecate'),
        dataTestid: 'schema-deprecate',
        class: 'text-destructive',
        command: () => {
          if (actionMenuSchema.value) confirmDeprecate(actionMenuSchema.value)
        },
      }]
    : []),
])

function openActionMenu(event: MouseEvent, schema: SchemaItem) {
  actionMenuSchema.value = schema
  actionMenuRef.value?.toggle(event)
}

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
  loadSchemaCounts()
})
</script>
