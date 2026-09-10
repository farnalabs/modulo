<template>
  <div class="page-wide py-8">
    <div v-if="loading" class="text-center py-12 text-muted-foreground">
      {{ $t('views.LibraryView.loading') }}
    </div>
    <div
      v-else-if="error"
      class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive"
      role="alert"
    >
      {{ error }}
    </div>
    <div v-else-if="collection" class="max-w-2xl mx-auto mt-6 space-y-4">
      <PageHeader :title="collection.name" />
      <div class="text-sm text-muted-foreground">
        @{{ collection.slug }} ·
        {{
          collection.status === 'published'
            ? $t('views.LibraryView.collection_status_published')
            : $t('views.LibraryView.collection_status_draft')
        }}
      </div>
      <p v-if="collection.description" class="text-sm">{{ collection.description }}</p>
      <div>
        <h3 class="text-sm font-medium mb-1">
          {{ $t('views.LibraryView.collection_manifest_pins') }}
        </h3>
        <ul v-if="(collection.manifest_pins || []).length" class="space-y-1">
          <li v-for="(pin, i) in collection.manifest_pins" :key="i" class="text-sm font-mono">
            {{ pin.slug }}@{{ pin.version }}
          </li>
        </ul>
        <p v-else class="text-sm text-muted-foreground">
          {{ $t('views.LibraryView.collection_empty_pins') }}
        </p>
      </div>
      <div v-if="collection.status === 'draft'" class="flex justify-end">
        <Button :disabled="publishing" data-testid="collection-publish" @click="publish">
          {{
            publishing
              ? $t('views.LibraryView.collection_publishing')
              : $t('views.LibraryView.collection_publish')
          }}
        </Button>
      </div>
      <div
        v-if="publishError"
        class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive"
        role="alert"
      >
        {{ publishError }}
      </div>

      <!-- Installs section (FAR-764) -->
      <div v-if="installs.length > 0" class="border-t pt-4 space-y-4">
        <h3 class="text-sm font-medium">
          {{ $t('views.CollectionDetail.installs_title') }}
        </h3>
        <div
          v-for="install in installs"
          :key="install.install_id"
          class="rounded-lg border bg-card p-4 space-y-3"
        >
          <div class="flex items-center justify-between">
            <div class="text-sm">
              <span class="font-medium">v{{ install.collection_version || '?' }}</span>
              <span class="ml-2 text-muted-foreground">
                {{ $t('views.CollectionDetail.install_status', { status: install.status }) }}
              </span>
            </div>
            <div class="flex items-center gap-2">
              <span
                v-if="install.community_sourced"
                class="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-300"
              >
                {{ $t('views.CollectionDetail.community_sourced') }}
              </span>
              <span
                v-if="install.agents_granted"
                class="inline-flex items-center gap-1 rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-800 dark:bg-green-900/30 dark:text-green-300"
              >
                {{ $t('views.CollectionDetail.agents_granted') }}
              </span>
              <Button
                v-if="install.community_sourced && !install.agents_granted"
                size="small"
                :disabled="granting === install.install_id"
                data-testid="collection-grant-agents"
                @click="grantAgents(install.install_id)"
              >
                {{
                  granting === install.install_id
                    ? $t('views.CollectionDetail.granting')
                    : $t('views.CollectionDetail.grant_agents')
                }}
              </Button>
            </div>
          </div>

          <!-- Connector checklist (FAR-762/764) -->
          <div
            v-if="install.connector_checklist && install.connector_checklist.length > 0"
            class="space-y-2"
          >
            <h4 class="text-xs font-medium text-muted-foreground uppercase tracking-wide">
              {{ $t('views.CollectionDetail.connector_checklist') }}
            </h4>
            <div
              v-for="(entry, ci) in install.connector_checklist"
              :key="ci"
              class="flex items-center justify-between rounded border px-3 py-2 text-sm"
            >
              <div class="flex items-center gap-2">
                <span
                  class="inline-block h-2 w-2 rounded-full"
                  :class="
                    entry.status === 'configured+bound'
                      ? 'bg-green-500'
                      : entry.status === 'configured'
                        ? 'bg-yellow-500'
                        : 'bg-red-500'
                  "
                />
                <span class="font-mono text-xs">{{ entry.connector_type_id }}</span>
              </div>
              <div class="flex items-center gap-2">
                <span class="text-xs text-muted-foreground">{{ entry.status }}</span>
                <Button
                  v-if="entry.status !== 'configured+bound'"
                  as="router-link"
                  :to="`/admin/connectors`"
                  size="small"
                  variant="ghost"
                  class="text-xs"
                  data-testid="collection-create-connector"
                >
                  {{ $t('views.CollectionDetail.create_connector') }}
                </Button>
              </div>
            </div>
          </div>

          <!-- Runnability -->
          <div v-if="install.runnable" class="text-xs text-green-600 dark:text-green-400">
            {{ $t('views.CollectionDetail.runnable') }}
          </div>
          <div v-else class="text-xs text-muted-foreground">
            {{ $t('views.CollectionDetail.not_runnable') }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import PageHeader from '../components/shared/PageHeader.vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'

interface CollectionPin {
  slug: string
  version: string
}

interface ConnectorChecklistEntry {
  connector_type_id: string
  status: string
}

interface CollectionInstall {
  install_id: string
  collection_id: string
  collection_version: string | null
  organisation_id: string
  status: string
  community_sourced: boolean
  agents_granted: boolean
  resolved_manifest: Record<string, unknown> | null
  connector_checklist: ConnectorChecklistEntry[] | null
  installed_entities: Record<string, unknown> | null
  runnable: boolean
  created_at: string
}

interface CollectionDetail {
  id: string
  name: string
  slug: string
  status: string | null
  description: string | null
  manifest_pins: CollectionPin[] | null
}

const route = useRoute()
const { t } = useI18n()

const collection = ref<CollectionDetail | null>(null)
const installs = ref<CollectionInstall[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const publishing = ref(false)
const publishError = ref<string | null>(null)
const granting = ref<string | null>(null)

async function load() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/libraries/{primitive_id}', {
      params: { path: { primitive_id: String(route.params.id) } },
    })
    if (err) {
      error.value = formatApiError(err)
      return
    }
    if (!data) {
      error.value = t('views.LibraryView.collection_failed_to_load')
      return
    }
    collection.value = data as unknown as CollectionDetail

    // Load installs for this collection.
    const { data: installData, error: installErr } = await api.GET(
      '/api/v1/libraries/collections/{primitive_id}/installs',
      {
        params: { path: { primitive_id: String(route.params.id) } },
      },
    )
    if (!installErr && installData) {
      const listResp = installData as unknown as { items: CollectionInstall[] }
      installs.value = listResp.items || []
    }
  } catch (e) {
    error.value = formatApiError(e)
  } finally {
    loading.value = false
  }
}

async function publish() {
  publishing.value = true
  publishError.value = null
  try {
    const { data, error: err } = await api.POST(
      '/api/v1/libraries/collections/{primitive_id}/publish',
      {
        params: { path: { primitive_id: String(route.params.id) } },
      },
    )
    if (err) {
      publishError.value = formatApiError(err)
      return
    }
    const updated = data as unknown as { status: string | null }
    if (collection.value) collection.value.status = updated.status
  } catch (e) {
    publishError.value = formatApiError(e)
  } finally {
    publishing.value = false
  }
}

async function grantAgents(installId: string) {
  granting.value = installId
  try {
    const { data, error: err } = await api.POST(
      '/api/v1/libraries/collections/{primitive_id}/installs/{install_id}/grant',
      {
        params: {
          path: {
            primitive_id: String(route.params.id),
            install_id: installId,
          },
        },
      },
    )
    if (err) {
      error.value = formatApiError(err)
      return
    }
    // Update the install in the list.
    const updated = data as unknown as CollectionInstall
    const idx = installs.value.findIndex((i) => i.install_id === installId)
    if (idx >= 0) {
      installs.value[idx] = updated
    }
  } catch (e) {
    error.value = formatApiError(e)
  } finally {
    granting.value = null
  }
}

onMounted(load)
</script>
