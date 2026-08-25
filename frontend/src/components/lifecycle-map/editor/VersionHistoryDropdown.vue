<template>
  <div class="relative" ref="dropdownRef">
    <button type="button"
      class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
      @click="open = !open"
    >
      <HistoryIcon class="h-3.5 w-3.5" />
      v{{ currentVersionNumber }}
      <ChevronDownIcon class="h-3 w-3" />
    </button>

    <div
      v-if="open"
      class="absolute left-0 top-full z-20 mt-1 w-64 rounded-lg border bg-card shadow-lg"
    >
      <div class="border-b px-3 py-2 text-xs font-medium text-muted-foreground">{{ $t('components.lifecycle-map.editor.VersionHistoryDropdown.version_history') }}</div>
      <div class="max-h-48 overflow-y-auto">
        <button type="button"
          v-for="v in sortedVersions"
          :key="v.id"
          :class="[
            'flex w-full items-center justify-between px-3 py-2 text-left text-xs hover:bg-accent',
            v.id === currentVersionId ? 'bg-accent font-medium' : '',
          ]"
          @click="selectVersion(v.id)"
        >
          <span>v{{ v.version_number }}</span>
          <span class="text-muted-foreground">{{ formatDate(v.created_at) }}</span>
        </button>
      </div>
      <div v-if="!versions.length" class="px-3 py-2 text-xs text-muted-foreground">
        {{ $t('components.lifecycle-map.editor.VersionHistoryDropdown.no_versions') }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { History as HistoryIcon, ChevronDown as ChevronDownIcon } from '@lucide/vue'
import type { LifecycleMapVersion } from '../../../types/lifecycleMap'
import { formatDateShort } from '../../../lib/formatDate'

const props = defineProps<{
  versions: LifecycleMapVersion[]
  currentVersionId: string
}>()

const emit = defineEmits<{
  select: [versionId: string]
}>()

const open = ref(false)
const dropdownRef = ref<HTMLElement | null>(null)

const currentVersionNumber = computed(() => {
  const v = props.versions.find(v => v.id === props.currentVersionId)
  return v?.version_number ?? 1
})

const sortedVersions = computed(() =>
  [...props.versions].sort((a, b) => b.version_number - a.version_number)
)

function formatDate(dateStr: string) {
  try {
    const d = new Date(dateStr)
    if (Number.isNaN(d.getTime())) return '?'
    return formatDateShort(d)
  } catch {
    return '?'
  }
}

function selectVersion(versionId: string) {
  emit('select', versionId)
  open.value = false
}

function onClickOutside(e: MouseEvent) {
  if (dropdownRef.value && !dropdownRef.value.contains(e.target as Node)) {
    open.value = false
  }
}

onMounted(() => document.addEventListener('click', onClickOutside))
onUnmounted(() => document.removeEventListener('click', onClickOutside))
</script>
