<template>
  <div
    class="card card-hover p-5 flex flex-col"
    :data-testid="`library-item-${prim.id}`"
  >
    <div class="flex items-start justify-between mb-3">
      <div>
        <span :class="typeBadgeClass(prim.primitive_type)">
          {{ prim.primitive_type }}
        </span>
        <h3 class="mt-2 text-base font-medium text-foreground">{{ prim.name }}</h3>
      </div>
      <span
        v-if="badge === 'modulo' && prim.source === 'modulo'"
        class="text-xs text-primary font-medium bg-primary/10 px-2 py-0.5 rounded"
      >
        {{ $t('views.LibraryView.modulo_badge') }}
      </span>
      <span
        v-else-if="badge === 'community' || (badge === 'modulo' && prim.source === 'community')"
        class="text-xs text-muted-foreground font-medium bg-muted px-2 py-0.5 rounded"
        data-testid="library-community-badge"
      >
        {{ $t('views.LibraryView.community_badge') }}
      </span>
      <span
        v-else-if="badge === 'preview'"
        class="badge badge-context-amber text-xs"
      >
        {{ $t('views.LibraryView.preview_badge') }}
      </span>
    </div>

    <p v-if="prim.description" class="text-sm text-muted-foreground flex-1 mb-4 line-clamp-2">
      {{ prim.description }}
    </p>

    <div v-if="showTags" class="flex items-center gap-2 flex-wrap mb-4">
      <span
        v-for="tag in (prim.tags || []).slice(0, 3)"
        :key="tag"
        class="text-xs bg-muted text-muted-foreground px-2 py-0.5 rounded"
      >
        {{ tag }}
      </span>
      <span v-if="(prim.tags || []).length > 3" class="text-xs text-muted-foreground">
        +{{ prim.tags.length - 3 }}
      </span>
    </div>

    <div v-if="showAutoUpdate && prim.forked_from" class="flex items-center gap-2 mb-3">
      <span class="text-xs text-muted-foreground">{{ $t('views.LibraryView.auto_update') }}</span>
      <button type="button"
        class="relative inline-flex h-5 w-9 items-center rounded-full transition-colors focus:outline-none disabled:opacity-50"
        :class="prim.auto_update ? 'bg-primary' : 'bg-muted'"
        role="switch"
        :aria-checked="prim.auto_update"
        :disabled="toggleLoading[prim.id]"
        @click="$emit('toggle-auto-update', prim)"
        :data-testid="`auto-update-toggle-${prim.id}`"
      >
        <span
          class="inline-block h-3.5 w-3.5 rounded-full bg-background transition-transform"
          :class="prim.auto_update ? 'translate-x-[18px]' : 'translate-x-[2px]'"
        />
      </button>
    </div>

    <div class="flex items-center gap-2 mt-auto">
      <button type="button"
        v-if="prim.primitive_type === 'pipeline_template' || prim.primitive_type === 'composite'"
        class="flex-1 px-3 py-2 border border-primary/30 hover:border-primary/60"
        @click="$emit('create-pipeline', prim)"
        data-testid="library-create-pipeline"
      >
        {{ $t('views.LibraryView.create_pipeline') }}
      </button>
      <button type="button"
        v-else-if="prim.primitive_type === 'lifecycle_map'"
        class="flex-1 px-3 py-2 border border-primary/30 hover:border-primary/60 disabled:opacity-50 disabled:cursor-not-allowed"
        :disabled="adapting[prim.id]"
        :aria-busy="adapting[prim.id]"
        @click="$emit('create-lifecycle-map', prim)"
        data-testid="library-create-lifecycle-map"
      >
        {{
          adapting[prim.id]
            ? $t('views.LibraryView.copy_to_adapt_creating')
            : $t('views.LibraryView.copy_to_adapt')
        }}
      </button>
      <button type="button"
        class="flex-1 px-3 py-2 border border-border bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
        @click="$emit('view-details', prim)"
        data-testid="library-view-details"
      >
        {{ $t('views.LibraryView.view_details') }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
export interface LibraryPrimitive {
  id: string
  source: string
  primitive_type: string
  name: string
  description: string | null
  tags: string[]
  forked_from: string | null
  auto_update: boolean
  tier?: 'native' | 'preview' | 'in_dev'
}

withDefaults(defineProps<{
  prim: LibraryPrimitive
  badge: 'modulo' | 'community' | 'preview'
  showTags?: boolean
  showAutoUpdate?: boolean
  toggleLoading?: Record<string, boolean>
  adapting?: Record<string, boolean>
}>(), {
  showTags: true,
  showAutoUpdate: false,
  toggleLoading: () => ({}),
  adapting: () => ({}),
})

defineEmits<{
  'create-pipeline': [prim: LibraryPrimitive]
  'create-lifecycle-map': [prim: LibraryPrimitive]
  'view-details': [prim: LibraryPrimitive]
  'toggle-auto-update': [prim: LibraryPrimitive]
}>()

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
</script>
