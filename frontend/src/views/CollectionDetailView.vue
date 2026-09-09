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
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import Button from 'primevue/button'
import PageHeader from '../components/shared/PageHeader.vue'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'

interface CollectionPin {
  slug: string
  version: string
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

const collection = ref<CollectionDetail | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const publishing = ref(false)
const publishError = ref<string | null>(null)

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
      error.value = $t('views.LibraryView.collection_failed_to_load')
      return
    }
    collection.value = data as unknown as CollectionDetail
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

onMounted(load)
</script>
