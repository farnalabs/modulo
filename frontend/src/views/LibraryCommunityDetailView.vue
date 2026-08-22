<template>
  <div class="min-h-screen">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="mx-auto flex items-center justify-between gap-3 max-w-6xl">
        <PageHeader :title="$t('views.LibraryCommunityDetail.title')" />
        <Button as="router-link" to="/library?section=hosted" class="px-4 py-1.5" data-testid="library-community-detail-back">
          {{ $t('views.LibraryCommunityDetail.back_to_library') }}
        </Button>
      </div>
    </header>

    <main class="page-wide">
      <div v-if="loading" class="text-center py-12 text-muted-foreground">{{ $t('views.LibraryCommunityDetail.loading') }}</div>

      <div
        v-else-if="error"
        class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive"
        role="alert"
        data-testid="library-community-detail-error"
      >
        {{ error }}
      </div>

      <div v-else-if="entry" class="card p-6 max-w-2xl" data-testid="library-community-detail">
        <div class="flex items-start justify-between mb-4">
          <div>
            <span :class="typeBadgeClass(entry.type)">{{ entry.type }}</span>
            <h1 class="mt-2 text-xl font-medium text-foreground">{{ entry.slug }}</h1>
          </div>
        </div>

        <dl class="grid grid-cols-1 sm:grid-cols-2 gap-4 text-sm">
          <div>
            <dt class="text-muted-foreground">{{ $t('views.LibraryCommunityDetail.author') }}</dt>
            <dd class="mt-0.5 text-foreground">{{ entry.author }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">{{ $t('views.LibraryCommunityDetail.version') }}</dt>
            <dd class="mt-0.5 text-foreground">{{ entry.version }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">{{ $t('views.LibraryCommunityDetail.license') }}</dt>
            <dd class="mt-0.5 text-foreground">{{ entry.license }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">{{ $t('views.LibraryCommunityDetail.status') }}</dt>
            <dd class="mt-0.5 text-foreground">{{ entry.status }}</dd>
          </div>
          <div v-if="entry.published_at">
            <dt class="text-muted-foreground">{{ $t('views.LibraryCommunityDetail.published_at') }}</dt>
            <dd class="mt-0.5 text-foreground">{{ formatDate(entry.published_at) }}</dd>
          </div>
        </dl>

        <div v-if="hasContent" class="mt-6">
          <h2 class="text-sm font-medium text-muted-foreground mb-2">{{ $t('views.LibraryCommunityDetail.content') }}</h2>
          <pre class="rounded-lg border bg-muted p-4 overflow-auto text-xs">{{ contentText }}</pre>
        </div>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import PageHeader from '../components/shared/PageHeader.vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'

type CommunityLibraryEntryDetail = components['schemas']['CommunityLibraryEntryDetail']

const route = useRoute()
const { t } = useI18n()

const entry = ref<CommunityLibraryEntryDetail | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)

const contentText = computed(() => {
  if (!entry.value?.content) return ''
  try {
    return JSON.stringify(entry.value.content, null, 2)
  } catch {
    return String(entry.value.content)
  }
})
const hasContent = computed(() => !!entry.value?.content && Object.keys(entry.value.content).length > 0)

function formatDate(value: string): string {
  const d = new Date(value)
  if (isNaN(d.getTime())) return value
  return d.toLocaleDateString()
}

function typeBadgeClass(type: string): string {
  const map: Record<string, string> = {
    pipeline_template: 'badge badge-context-blue',
    workflow: 'badge badge-context-teal',
    agent: 'badge badge-context-purple',
    schema: 'badge badge-context-amber',
    integration: 'badge badge-context-cyan',
    test_fixture: 'badge badge-context-pink',
    composite: 'badge badge-context-green',
    lifecycle_map: 'badge badge-context-blue',
  }
  return map[type] ?? 'badge badge-context-slate'
}

async function loadEntry(): Promise<void> {
  const id = typeof route.params.id === 'string' ? route.params.id : null
  if (!id) {
    error.value = t('views.LibraryCommunityDetail.missing_entry_id')
    return
  }
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/libraries/community/{entry_id}', {
      params: { path: { entry_id: id } },
    })
    if (err) {
      error.value = formatApiError(err)
      return
    }
    entry.value = data
  } catch (e) {
    error.value = formatApiError(e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadEntry()
})
</script>
