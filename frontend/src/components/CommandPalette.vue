<template>
  <Teleport to="body">
    <div v-if="isOpen" class="fixed inset-0 z-50 flex items-start justify-center pt-[15vh]">
      <div class="fixed inset-0 bg-black/50" @click="close" aria-hidden="true" />
      <div
        class="relative z-10 w-full max-w-lg rounded-lg border bg-background shadow-xl"
        role="dialog"
        aria-label="Command palette"
        aria-modal="true"
      >
        <div class="flex items-center gap-2 border-b px-4">
          <SvgIcon name="Search" class="h-4 w-4 shrink-0 text-muted-foreground" />
          <input
            id="commandpalette-search-input"
            ref="inputRef"
            v-model="query"
            :aria-label="$t('components.AppLayout.search_pages')"
            placeholder="Search pages..."
            class="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            @keydown.down.prevent="selectedIndex = Math.min(selectedIndex + 1, filteredItems.length - 1)"
            @keydown.up.prevent="selectedIndex = Math.max(selectedIndex - 1, 0)"
            @keydown.enter.prevent="navigate"
          />
        </div>
        <div class="max-h-80 overflow-y-auto p-2" data-cmdk-container>
          <div
            v-if="filteredItems.length === 0 && query.length > 0"
            class="px-3 py-8 text-center text-sm text-muted-foreground"
          >
            No results found
          </div>
          <button type="button"
            v-for="(item, idx) in filteredItems"
            :key="item.path + item.label"
            :class="[
              'flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left text-sm transition-colors',
              idx === selectedIndex ? 'bg-accent text-accent-foreground' : 'text-foreground',
            ]"
            @click="goTo(item.path)"
            @mouseenter="selectedIndex = idx"
            @focus="selectedIndex = idx"
          >
            <SvgIcon :name="item.icon" class="h-4 w-4 shrink-0 text-muted-foreground" />
            <span class="flex-1 truncate">{{ item.label }}</span>
            <span v-if="item.section" class="shrink-0 text-xs text-muted-foreground">{{ item.section }}</span>
          </button>
        </div>
        <div class="border-t px-4 py-2 text-xs text-muted-foreground text-right">
          &uarr;&darr; navigate &middot; &crarr; open &middot; Esc close
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { getNavGroups, getVisibleNavGroups } from '../config/navigation'
import SvgIcon from './SvgIcon.vue'
import { useNavVisibilityContext } from '../composables/useNavVisibilityContext'

interface SearchItem {
  label: string
  path: string
  icon: string
  section: string
}

const router = useRouter()
const navContext = useNavVisibilityContext()
const isOpen = ref(false)
const query = ref('')
const selectedIndex = ref(0)
const inputRef = ref<HTMLInputElement | null>(null)

const searchItems = computed<SearchItem[]>(() => {
  const seen = new Set<string>()
  const items: SearchItem[] = []
  const ctx = navContext.value
  const visibleGroups = getVisibleNavGroups(ctx)
  const visiblePaths = new Set(visibleGroups.flatMap((g) => g.items.map((i) => i.to)))
  for (const group of visibleGroups) {
    for (const navItem of group.items) {
      const path = navItem.to
      if (seen.has(path)) continue
      seen.add(path)
      items.push({
        label: navItem.label,
        path,
        icon: navItem.icon,
        section: group.label,
      })
    }
  }
  // Curated parent/landing routes that are not sidebar items in the manifest
  // (e.g. /settings, /admin/system). Gate them with the SAME navbar rules:
  // if a path resolves to a manifest item it must appear in the visible set
  // (group-level systemAdminOnly + item-level gates already applied),
  // otherwise it stays hidden. Parent routes with no manifest entry are
  // landing pages and are always shown.
  const extras: SearchItem[] = [
    { label: 'Dashboard', path: '/', icon: 'LayoutDashboard', section: 'BUILD' },
    { label: 'Pipelines', path: '/pipelines', icon: 'GitFork', section: 'BUILD' },
    { label: 'Library', path: '/library', icon: 'BookOpen', section: 'BUILD' },
    { label: 'Runs', path: '/runs', icon: 'CirclePlay', section: 'BUILD' },
    { label: 'Evals', path: '/evals', icon: 'CheckSquare', section: 'MONITOR' },
    { label: 'Schemas', path: '/schemas', icon: 'Database', section: 'CONFIGURE' },
    { label: 'Connectors', path: '/admin/connectors', icon: 'Plug', section: 'ADMIN' },
    { label: 'Settings', path: '/settings', icon: 'Settings', section: 'ADMIN' },
    { label: 'Organization', path: '/admin/org', icon: 'Building', section: 'ADMIN' },
    { label: 'System', path: '/admin/system', icon: 'Settings', section: 'ADMIN' },
    { label: 'Cost Management', path: '/admin/costs', icon: 'DollarSign', section: 'ADMIN' },
  ]
  const rawItems = getNavGroups().flatMap((g) => g.items)
  for (const extra of extras) {
    if (seen.has(extra.path)) continue
    const navItem = rawItems.find((i) => i.to === extra.path)
    if (navItem && !visiblePaths.has(extra.path)) continue
    seen.add(extra.path)
    items.push(extra)
  }
  return items
})

const filteredItems = computed(() => {
  const q = query.value.toLowerCase().trim()
  if (!q) return searchItems.value
  return searchItems.value.filter(
    item =>
      item.label.toLowerCase().includes(q) ||
      item.section.toLowerCase().includes(q),
  )
})

watch(selectedIndex, (idx) => {
  nextTick(() => {
    const container = document.querySelector('[data-cmdk-container]')
    if (!container) return
    const buttons = container.querySelectorAll('button')
    buttons[idx]?.scrollIntoView({ block: 'nearest' })
  })
})

watch(filteredItems, () => {
  const count = filteredItems.value.length
  if (count === 0) {
    selectedIndex.value = 0
  } else if (selectedIndex.value > count - 1) {
    selectedIndex.value = count - 1
  }
})

function open() {
  isOpen.value = true
  query.value = ''
  selectedIndex.value = 0
  nextTick(() => {
    inputRef.value?.focus()
  })
}

function close() {
  isOpen.value = false
}

defineExpose({ open })

function navigate() {
  const items = filteredItems.value
  if (items.length === 0) return
  const item = items[selectedIndex.value]
  if (item) goTo(item.path)
}

function goTo(path: string) {
  close()
  router.push(path)
}

function handleKeydown(e: KeyboardEvent) {
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
    e.preventDefault()
    if (isOpen.value) {
      close()
    } else {
      open()
    }
  }
}

onMounted(() => {
  document.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
})
</script>
