<template>
  <div class="min-h-screen bg-background">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="mx-auto flex items-center justify-between gap-3 max-w-6xl">
        <div class="flex items-center gap-3">
          <router-link
            to="/lifecycle-maps"
            class="text-sm text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
            Back
          </router-link>
          <PageHeader :title="mapData?.name || 'Lifecycle Map'" />
          <span
            v-if="mapData"
            class="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
          >
            v{{ mapData.current_version }}
          </span>
        </div>
        <div v-if="mapData?.versions && mapData.versions.length > 1" class="flex items-center gap-2">
          <label for="lifecyclemapview-field-1" class="text-sm text-muted-foreground">Version:</label>
          <Select v-model="selectedVersion" @update:model-value="onVersionChange">
            <SelectTrigger data-testid="lifecycle-map-version-select" class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm">
              <SelectValue placeholder="Select version" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="v in sortedVersions" :key="v.version" :value="v.version">
                v{{ v.version }}
                <template v-if="v.created_by"> — {{ v.created_by }}</template>
              </SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>
    </header>

    <main class="page-wide">
      <div v-if="isLoadingDetail" class="flex items-center justify-center py-20">
        <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
      </div>

      <ErrorAlert
        v-else-if="detailError"
        :message="detailError"
        :on-retry="() => loadMap()"
        class="mb-6"
      />

      <template v-else-if="mapData">
        <div class="mb-4 flex items-start justify-between gap-4">
          <div class="flex-1">
            <p v-if="mapData.description" class="text-sm text-muted-foreground">
              {{ mapData.description }}
            </p>
            <div class="flex items-center gap-3 mt-2 text-xs text-muted-foreground">
              <span v-if="mapData.owner" class="flex items-center gap-1">
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
                {{ mapData.owner }}
              </span>
              <span>{{ (mapData.stages ?? []).length }} stages</span>
              <span>{{ graduatedCount }} graduated</span>
              <span>{{ manualCount }} manual</span>
            </div>
          </div>
        </div>

        <div class="rounded-xl border border-border bg-card overflow-hidden" style="height: 600px">
          <LifecycleMapRenderer
            :map-data="mapData"
            :on-modulo-stage-click="handleModuloStageClick"
            :on-external-stage-click="handleExternalStageClick"
          />
        </div>
      </template>

      <div v-else class="text-center py-20 text-muted-foreground">
        Lifecycle map not found.
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHeader from '../../components/shared/PageHeader.vue'
import { useLifecycleMapsStore } from '../../stores/lifecycleMaps'
import LifecycleMapRenderer from '../../components/lifecycle-map/LifecycleMapRenderer.vue'
import ErrorAlert from '../../components/shared/ErrorAlert.vue'
import type { LifecycleMapStage } from '../../stores/lifecycleMaps'
import { Select, SelectTrigger, SelectContent, SelectItem, SelectValue } from '@/components/ui/select'

const route = useRoute()
const router = useRouter()
const store = useLifecycleMapsStore()

const mapId = computed(() => route.params.id as string)
const selectedVersion = ref<number | null>(null)

const mapData = computed(() => store.currentMap)
const isLoadingDetail = computed(() => store.isLoadingDetail)
const detailError = computed(() => store.detailError)
const graduatedCount = computed(() => store.graduatedCount)
const manualCount = computed(() => store.manualCount)

const sortedVersions = computed(() => {
  const versions = mapData.value?.versions ?? []
  return [...versions].sort((a, b) => b.version - a.version)
})

async function loadMap(): Promise<void> {
  if (!mapId.value) return
  await store.fetchMap(mapId.value)
  if (store.currentMap) {
    selectedVersion.value = store.currentMap.current_version
  }
}

async function onVersionChange(): Promise<void> {
  if (!mapId.value || selectedVersion.value == null) return
  if (selectedVersion.value === mapData.value?.current_version) {
    await store.fetchMap(mapId.value)
  } else {
    await store.fetchMapVersion(mapId.value, selectedVersion.value)
  }
}

function handleModuloStageClick(stage: LifecycleMapStage): void {
  if (stage.pipeline_id) {
    router.push({ name: 'pipeline-editor', params: { id: stage.pipeline_id } })
  }
}

function handleExternalStageClick(stage: LifecycleMapStage): void {
  if (stage.external_url) {
    window.open(stage.external_url, '_blank', 'noopener,noreferrer')
  }
}

onMounted(() => {
  if (mapId.value) {
    store.fetchMap(mapId.value).then(() => {
      if (store.currentMap) {
        selectedVersion.value = store.currentMap.current_version
      }
    })
  }
})
</script>
