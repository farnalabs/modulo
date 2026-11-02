<template>
  <div class="h-[calc(100vh-3.5rem)] bg-background flex flex-col">
    <div class="page-wide">
      <PageHeader
        :title="$t('views.SystemMapsView.title')"
        :subtitle="$t('views.SystemMapsView.subtitle')"

      >
        <template #right>
          <button type="button" class="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground" @click="openCreateDialog" data-testid="system-maps-create">{{ $t('views.SystemMapsView.create_map') }}</button>
        </template>
      </PageHeader>
    </div>

    <div class="flex flex-1 min-h-0">
      <main class="flex-1 page-wide min-w-0 overflow-y-auto">
        <ErrorAlert v-if="error" :message="error" :on-retry="loadMaps" />

        <template v-else-if="loading">
          <div class="overflow-x-auto rounded-lg border" aria-hidden="true">
            <table class="w-full text-left text-sm">
              <thead class="bg-muted/50">
                <tr>
                  <th class="px-4 py-3 font-medium">{{ $t('views.SystemMapsView.name') }}</th>
                  <th class="px-4 py-3 font-medium">{{ $t('views.SystemMapsView.description') }}</th>
                  <th class="px-4 py-3 font-medium">{{ $t('views.SystemMapsView.status') }}</th>
                  <th class="px-4 py-3 font-medium">{{ $t('views.SystemMapsView.revisions') }}</th>
                  <th class="px-4 py-3 font-medium">{{ $t('views.SystemMapsView.last_updated') }}</th>
                  <th class="px-4 py-3 font-medium text-right">{{ $t('views.SystemMapsView.actions') }}</th>
                </tr>
              </thead>
              <tbody class="divide-y">
                <tr v-for="row in 5" :key="row">
                  <td class="px-4 py-3"><div class="h-4 w-48 rounded bg-muted/50" /></td>
                  <td class="px-4 py-3"><div class="h-4 w-full max-w-md rounded bg-muted/50" /></td>
                  <td class="px-4 py-3"><div class="h-4 w-20 rounded bg-muted/50" /></td>
                  <td class="px-4 py-3"><div class="h-4 w-12 rounded bg-muted/50" /></td>
                  <td class="px-4 py-3"><div class="h-4 w-28 rounded bg-muted/50" /></td>
                  <td class="px-4 py-3"><div class="ml-auto h-4 w-20 rounded bg-muted/50" /></td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>

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
                    <th class="px-4 py-3 font-medium">{{ $t('views.SystemMapsView.revisions') }}</th>
                    <th class="px-4 py-3 font-medium">{{ $t('views.SystemMapsView.last_updated') }}</th>
                    <th class="px-4 py-3 font-medium text-right">{{ $t('views.SystemMapsView.actions') }}</th>
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
                    <td class="px-4 py-3 text-muted-foreground">
                      {{ $t('views.SystemMapsView.updated_at') }}
                    </td>
                    <td class="px-4 py-3 text-muted-foreground">{{ formatDate(map.updated_at) }}</td>
                    <td class="px-4 py-3 text-right">
                      <button
                        type="button"
                        class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                        :data-testid="`system-map-action-${map.id}`"
                        :aria-label="$t('views.SystemMapsView.actions', { name: map.name })"
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

    <!-- Action Menu -->
    <Transition name="fade">
      <div v-if="actionMenu.map" class="fixed z-50 rounded border bg-card shadow-lg" :data-testid="`system-map-action-menu`">
        <ul class="py-1">
          <li @click="openMap(actionMenu.map!); actionMenu = { map: null, target: null }" class="px-3 py-2 text-sm hover:bg-accent cursor-pointer" data-testid="system-map-action-view">{{ $t('views.SystemMapsView.view') }}</li>
          <li @click="openMap(actionMenu.map!); actionMenu = { map: null, target: null }" class="px-3 py-2 text-sm hover:bg-accent cursor-pointer" data-testid="system-map-action-edit">{{ $t('views.SystemMapsView.edit') }}</li>
          <li @click="runDiscovery(actionMenu.map!); actionMenu = { map: null, target: null }" class="px-3 py-2 text-sm hover:bg-accent cursor-pointer" data-testid="system-map-action-discover">{{ $t('views.SystemMapsView.discover') }}</li>
          <li class="border-t my-1" />
          <li @click="confirmDelete(actionMenu.map!); actionMenu = { map: null, target: null }" class="px-3 py-2 text-sm text-destructive hover:bg-accent cursor-pointer" data-testid="system-map-action-delete">{{ $t('views.SystemMapsView.delete') }}</li>
        </ul>
      </div>
    </Transition>

    <!-- Delete Confirmation -->
    <Transition name="fade">
      <div v-if="deleteDialog.map" class="fixed inset-0 z-50 flex items-center justify-center" :data-testid="`system-map-delete-confirm`">
        <div class="absolute inset-0 bg-black/50" @click="deleteDialog = { map: null }" />
        <div class="relative z-10 rounded-lg bg-card p-6 w-full max-w-md">
          <h3 class="font-semibold">{{ $t('views.SystemMapsView.delete') }}</h3>
          <p class="mt-2 text-sm text-muted-foreground">{{ $t('views.SystemMapsView.delete_confirm', { name: deleteDialog.map.name }) }}</p>
          <div class="mt-4 flex justify-end gap-2">
            <button type="button" class="rounded border px-3 py-1.5 text-sm" @click="deleteDialog = { map: null }" data-testid="system-map-delete-cancel">{{ $t('common.cancel') }}</button>
            <button type="button" class="rounded bg-destructive px-3 py-1.5 text-sm text-destructive-foreground" @click="executeDelete(deleteDialog.map!); deleteDialog = { map: null }" data-testid="system-map-delete-confirm-btn">{{ $t('common.delete') }}</button>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useSystemMapsStore } from '../stores/systemMaps'
import PageHeader from '../components/shared/PageHeader.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import { MoreVertical } from '@lucide/vue'
import type { SystemMap } from '../stores/systemMaps'

const router = useRouter()
const store = useSystemMapsStore()

const createDialogOpen = ref(false)
const createForm = ref({ name: '', description: '' })
const creating = ref(false)
const nameId = 'system-map-create-name'
const descId = 'system-map-create-description'
const actionMenu = ref<{ map: SystemMap | null; target: EventTarget | null }>({ map: null, target: null })
const deleteDialog = ref<{ map: SystemMap | null }>({ map: null })

const maps = computed(() => store.maps)
const total = computed(() => store.total)
const page = computed(() => store.page)
const pageSize = computed(() => store.pageSize)
const loading = computed(() => store.loading)
const error = computed(() => store.error)

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
  try {
    await store.createMap(createForm.value.name.trim(), createForm.value.description.trim())
    createDialogOpen.value = false
  } catch (err) {
    console.error('Create map failed:', err)
  } finally { creating.value = false }
}

function openActionMenu(event: Event, map: SystemMap): void {
  actionMenu.value = { map, target: event.currentTarget }
}

async function runDiscovery(map: SystemMap): Promise<void> {
  try {
    await store.discover(`Discovered ${map.name}`, `Discovery from ${new Date().toISOString()}`)
    await router.push(`/system-maps/${store.activeMap?.id ?? map.id}`)
  } catch (err) {
    console.error('Discovery failed:', err)
  }
}

function confirmDelete(map: SystemMap): void { deleteDialog.value = { map } }

async function executeDelete(map: SystemMap): Promise<void> {
  try { await store.deleteMap(map.id) } catch (err) { console.error('Delete failed:', err) }
}

onMounted(async () => { await store.fetchAccess(); await loadMaps() })
</script>
