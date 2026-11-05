<template>
  <div class="h-[calc(100vh-3.5rem)] bg-background flex flex-col">
    <div class="page-wide">
      <PageHeader
        :title="$t('views.SystemMapsView.title')"
        :subtitle="$t('views.SystemMapsView.subtitle')"
      >
        <template #right>
          <button v-if="store.hasAccess" type="button" class="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground" @click="openCreateDialog" data-testid="system-maps-create">{{ $t('views.SystemMapsView.create_map') }}</button>
          <span v-else-if="store.hasReadAccess" class="rounded bg-muted px-2.5 py-1.5 text-xs text-muted-foreground" data-testid="system-maps-read-only">{{ $t('views.SystemMapsView.read_only') }}</span>
        </template>
      </PageHeader>
    </div>

    <div class="flex flex-1 min-h-0">
      <main class="flex-1 page-wide min-w-0 overflow-y-auto">
        <ErrorAlert
          v-if="actionError"
          :message="actionError"
          :on-dismiss="clearActionError"
          :dismiss-label="$t('common.dismiss')"
          class="mb-3"
        />

        <!-- A 402 feature_required is an entitlement answer, not a transient
             failure: show the honest unavailable state and never offer a retry
             that cannot succeed. -->
        <div
          v-if="unavailable"
          class="rounded-lg border bg-muted/40 p-4 text-sm text-muted-foreground"
          aria-live="polite"
        >
          {{ $t('views.SystemMapsView.unavailable') }}
        </div>

        <ErrorAlert v-else-if="error" :message="error" :on-retry="loadMaps" />

        <TableSkeleton
          v-else-if="loading"
          :columns="store.hasAccess ? 5 : 4"
          :rows="5"
          :aria-label="$t('common.loading')"
          data-testid="system-maps-loading"
        />

        <template v-else>
          <EmptyState v-if="maps.length === 0" :title="$t('views.SystemMapsView.no_maps')" :description="$t('views.SystemMapsView.empty_hint')" />

          <template v-else>
            <div class="overflow-x-auto rounded-lg border">
              <table class="w-full text-left text-sm">
                <thead class="bg-muted/50">
                  <tr>
                    <th class="px-4 py-3 font-medium">{{ $t('views.SystemMapsView.name') }}</th>
                    <th class="px-4 py-3 font-medium">{{ $t('views.SystemMapsView.description') }}</th>
                    <th class="px-4 py-3 font-medium">{{ $t('views.SystemMapsView.status') }}</th>
                    <th class="px-4 py-3 font-medium">{{ $t('views.SystemMapsView.last_updated') }}</th>
                    <th v-if="store.hasAccess" class="px-4 py-3 font-medium text-right">{{ $t('views.SystemMapsView.actions_header') }}</th>
                  </tr>
                </thead>
                <tbody class="divide-y">
                  <tr
                    v-for="map in maps"
                    :key="map.id"
                    class="cursor-pointer hover:bg-muted/30 transition-colors"
                    tabindex="0"
                    @click="openMap(map)"
                    @keydown.enter="openMap(map)"
                    @keydown.space.prevent="openMap(map)"
                    :data-testid="`system-map-row-${map.id}`"
                  >
                    <td class="px-4 py-3 font-medium">{{ map.name }}</td>
                    <td class="px-4 py-3 text-muted-foreground">{{ map.description ?? '—' }}</td>
                    <td class="px-4 py-3">
                      <span class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium" :class="map.published_revision_id ? 'bg-success/10 text-success-text' : 'bg-warning/10 text-warning-text'">
                        <span class="h-1.5 w-1.5 rounded-full" :class="map.published_revision_id ? 'bg-success' : 'bg-warning'" aria-hidden="true" />
                        {{ $t('views.SystemMapsView.' + (map.published_revision_id ? 'published' : 'draft')) }}
                      </span>
                    </td>
                    <td class="px-4 py-3 text-muted-foreground">{{ formatDate(map.updated_at) }}</td>
                    <td v-if="store.hasAccess" class="px-4 py-3 text-right">
                      <button
                        type="button"
                        class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                        :data-testid="`system-map-action-${map.id}`"
                        :aria-label="$t('views.SystemMapsView.actions', { name: map.name })"
                        aria-haspopup="menu"
                        @click.stop="openActionMenu($event, map)"
                      >
                        <MoreVertical class="h-3.5 w-3.5" aria-hidden="true" />
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>

            <div v-if="total > pageSize" class="flex items-center justify-between mt-4 px-4 pb-4" data-testid="system-maps-pagination">
              <span class="text-sm text-muted-foreground">{{ $t('views.SystemMapsView.page_of', { page: page, total: Math.ceil(total / pageSize) }) }}</span>
              <div class="flex gap-1">
                <button type="button" class="rounded border px-2 py-1 text-sm" :disabled="page <= 1" @click="loadMaps(page - 1)" :data-testid="`system-maps-prev`">{{ $t('common.previous') }}</button>
                <button type="button" class="rounded border px-2 py-1 text-sm" :disabled="page >= Math.ceil(total / pageSize)" @click="loadMaps(page + 1)" :data-testid="`system-maps-next`">{{ $t('common.next') }}</button>
              </div>
            </div>
          </template>
        </template>
      </main>
    </div>

    <Menu ref="actionMenuRef" :key="actionMenuKey" :model="actionMenuItems" popup />

    <!-- Create Map Dialog -->
    <Dialog v-model:visible="createDialogOpen" :modal="true" :dismissable-mask="true" :style="{ width: '28rem' }" :header="$t('views.SystemMapsView.create_map')" data-testid="system-map-create-dialog">
      <form @submit.prevent="submitCreate" class="space-y-4">
        <div>
          <label :for="nameId" class="block text-sm font-medium mb-1">{{ $t('views.SystemMapsView.name') }} <span class="text-destructive">*</span></label>
          <InputText :id="nameId" v-model="createForm.name" :placeholder="$t('views.SystemMapsView.name_placeholder')" :aria-required="true" data-testid="system-map-create-name" />
        </div>
        <div>
          <label :for="descId" class="block text-sm font-medium mb-1">{{ $t('views.SystemMapsView.description') }}</label>
          <Textarea :id="descId" v-model="createForm.description" :placeholder="$t('views.SystemMapsView.description_placeholder')" rows="3" data-testid="system-map-create-description" />
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" class="rounded-md border px-3 py-2 text-sm font-medium" @click="createDialogOpen = false" data-testid="system-map-create-cancel">{{ $t('common.cancel') }}</button>
          <button type="submit" class="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground" :disabled="creating" data-testid="system-map-create-submit">{{ creating ? $t('common.creating') : $t('common.create') }}</button>
        </div>
      </form>
    </Dialog>

    <!-- Delete Confirmation -->
    <FormDialog
      v-model:open="deleteDialogOpen"
      :title="$t('views.SystemMapsView.delete')"
      :description="deleteDialog.map ? $t('views.SystemMapsView.delete_confirm', { name: deleteDialog.map.name }) : ''"
      :confirmText="$t('common.delete')"
      confirmSeverity="danger"
      :loading="deleting"
      cancelTestId="system-map-delete-cancel"
      confirmTestId="system-map-delete-confirm-btn"
      @confirm="executeDelete"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useSystemMapsStore } from '../stores/systemMaps'
import PageHeader from '../components/shared/PageHeader.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import TableSkeleton from '../components/shared/TableSkeleton.vue'
import FormDialog from '../components/shared/FormDialog.vue'
import Dialog from 'primevue/dialog'
import Menu from 'primevue/menu'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import { MoreVertical } from '@lucide/vue'
import type { SystemMap } from '../stores/systemMaps'

const router = useRouter()
const store = useSystemMapsStore()
const { t } = useI18n()

const createDialogOpen = ref(false)
const createForm = ref({ name: '', description: '' })
const creating = ref(false)
const nameId = 'system-map-create-name'
const descId = 'system-map-create-description'
const actionMenuRef = ref<InstanceType<typeof Menu> | null>(null)
const actionMenuKey = ref(0)
const actionMenuMap = ref<SystemMap | null>(null)
const deleteDialogOpen = ref(false)
const deleteDialog = ref<{ map: SystemMap | null }>({ map: null })
const deleting = ref(false)
const actionError = ref<string | null>(null)

const maps = computed(() => store.maps)
const total = computed(() => store.total)
const page = computed(() => store.page)
const pageSize = computed(() => store.pageSize)
const loading = computed(() => store.loading)
const error = computed(() => store.error)
const unavailable = computed(() => store.unavailable)

const actionMenuItems = computed(() => {
  const map = actionMenuMap.value
  if (!map) return []
  if (!store.hasAccess) return [{ label: t('views.SystemMapsView.view'), command: () => openMap(map) }]
  return [
    { label: t('views.SystemMapsView.view'), command: () => openMap(map) },
    { label: t('views.SystemMapsView.edit'), command: () => openMap(map) },
    { label: t('views.SystemMapsView.discover'), command: () => runDiscovery(map) },
    { separator: true },
    { label: t('views.SystemMapsView.delete'), class: 'text-destructive', command: () => confirmDelete(map) },
  ]
})

function clearActionError(): void { actionError.value = null }
function formatDate(iso: string): string { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }

async function loadMaps(nextPage = 1): Promise<void> {
  await store.fetchMaps(nextPage)
}

async function openMap(map: SystemMap): Promise<void> { await router.push(`/system-maps/${map.id}`) }

async function openCreateDialog(): Promise<void> {
  createForm.value = { name: '', description: '' }
  createDialogOpen.value = true
}

async function submitCreate(): Promise<void> {
  if (!createForm.value.name.trim()) return
  creating.value = true
  actionError.value = null
  try {
    await store.createMap(createForm.value.name.trim(), createForm.value.description.trim())
    createDialogOpen.value = false
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('views.SystemMapsView.create_failed')
  } finally { creating.value = false }
}

function openActionMenu(event: Event, map: SystemMap): void {
  actionMenuMap.value = map
  actionMenuKey.value += 1
  actionMenuRef.value?.toggle(event)
}

async function runDiscovery(map: SystemMap): Promise<void> {
  actionError.value = null
  try {
    await store.discover(`Discovered ${map.name}`, `Discovery from ${new Date().toISOString()}`)
    await router.push(`/system-maps/${store.activeMap?.id ?? map.id}`)
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('views.SystemMapsView.discover')
  }
}

function confirmDelete(map: SystemMap): void {
  deleteDialog.value = { map }
  deleteDialogOpen.value = true
}

async function executeDelete(): Promise<void> {
  const map = deleteDialog.value.map
  if (!map) return
  deleting.value = true
  actionError.value = null
  try {
    await store.deleteMap(map.id)
    deleteDialogOpen.value = false
    deleteDialog.value = { map: null }
  } catch (err) {
    actionError.value = err instanceof Error ? err.message : t('views.SystemMapsView.delete_failed')
  } finally { deleting.value = false }
}

onMounted(async () => { await store.fetchAccess(); await loadMaps() })
</script>
