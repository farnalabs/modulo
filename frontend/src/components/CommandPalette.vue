<template>
  <Teleport to="body">
    <div v-if="isOpen" class="fixed inset-0 z-50 flex items-start justify-center pt-[15vh]">
      <div class="fixed inset-0 bg-black/50" @click="close" @keydown.escape="close" aria-hidden="true" />
      <div
        class="relative z-10 w-full max-w-lg rounded-lg border bg-background shadow-xl"
        role="dialog"
        :aria-label="$t('components.CommandPalette.aria_label')"
        aria-modal="true"
      >
        <div class="flex items-center gap-2 border-b px-4">
          <SvgIcon name="Search" class="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <input
            id="commandpalette-search-input"
            ref="inputRef"
            v-model="query"
            role="combobox"
            aria-autocomplete="list"
            aria-expanded="true"
            aria-controls="cmdk-listbox"
            :aria-activedescendant="activeDescendantId"
            :aria-label="$t('components.CommandPalette.aria_label')"
            :placeholder="$t('components.CommandPalette.placeholder')"
            class="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            @keydown.down.prevent="selectedIndex = Math.min(selectedIndex + 1, allItems.length - 1)"
            @keydown.up.prevent="selectedIndex = Math.max(selectedIndex - 1, 0)"
            @keydown.enter.prevent="navigate"
          />
        </div>
        <!-- Announce result-count/loading changes to screen readers. The list
             rows carry role="option" and the input is a combobox, so sighted
             keyboard users already see the highlight; this keeps AT users in
             sync without moving focus out of the input. -->
        <p class="sr-only" role="status" aria-live="polite">{{ statusText }}</p>
        <div class="max-h-80 overflow-y-auto p-2">
          <!-- Status lines are additive: navigation matches stay usable while a
               user-data provider is loading or has failed. -->
          <div
            v-if="loading"
            class="px-3 py-2 text-center text-xs text-muted-foreground"
          >
            {{ $t('components.CommandPalette.searching') }}
          </div>
          <div
            v-if="error"
            role="alert"
            class="px-3 py-2 text-center text-sm text-destructive"
          >
            {{ $t('components.CommandPalette.search_error') }}
          </div>
          <div
            v-if="!loading && !error && allItems.length === 0 && query.length > 0"
            class="px-3 py-8 text-center text-sm text-muted-foreground"
          >
            {{ $t('components.CommandPalette.no_results') }}
          </div>
          <div
            id="cmdk-listbox"
            class="contents"
            role="listbox"
            :aria-label="$t('components.CommandPalette.results_list_label')"
            data-cmdk-container
          >
            <button type="button"
              v-for="(item, idx) in allItems"
              :id="'cmdk-option-' + idx"
              :key="item.id"
              role="option"
              :aria-selected="idx === selectedIndex"
              :class="[
                'flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left text-sm transition-colors',
                idx === selectedIndex ? 'bg-accent text-accent-foreground' : 'text-foreground',
              ]"
              @click="goTo(item.path)"
              @mouseenter="selectedIndex = idx"
              @focus="selectedIndex = idx"
            >
              <SvgIcon :name="item.icon" class="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              <span class="flex min-w-0 flex-1 flex-col">
                <span class="truncate">{{ item.label }}</span>
                <span v-if="item.subtitle" class="truncate text-xs text-muted-foreground">{{ item.subtitle }}</span>
              </span>
              <span v-if="sectionLabel(item)" class="shrink-0 text-xs text-muted-foreground">{{ sectionLabel(item) }}</span>
            </button>
          </div>
        </div>
        <div class="border-t px-4 py-2 text-xs text-muted-foreground text-right">
          {{ $t('components.CommandPalette.footer_hint') }}
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { getNavGroups, getVisibleNavGroups } from '../config/navigation'
import SvgIcon from './SvgIcon.vue'
import { useNavVisibilityContext } from '../composables/useNavVisibilityContext'
import type { PaletteItem } from '../lib/commandPalette/providers'
import { recordRecentVisit } from '../lib/commandPalette/recents'
import { useCommandSearch } from '../composables/useCommandSearch'

const MAX_RESULTS = 50

const router = useRouter()
const route = useRoute()
const { t } = useI18n()
const navContext = useNavVisibilityContext()
const isOpen = ref(false)
const query = ref('')
const selectedIndex = ref(0)
const inputRef = ref<HTMLInputElement | null>(null)

// Phase 1: the navigation-item index (sidebar items plus curated landing
// routes that have no sidebar entry). Permission/tenancy gating for these is
// the manifest + isNavItemVisible path already used by the sidebar.
const navItems = computed<PaletteItem[]>(() => {
  const seen = new Set<string>()
  const items: PaletteItem[] = []
  const ctx = navContext.value
  const visibleGroups = getVisibleNavGroups(ctx)
  const visiblePaths = new Set(visibleGroups.flatMap((g) => g.items.map((i) => i.to)))
  for (const group of visibleGroups) {
    for (const navItem of group.items) {
      const path = navItem.to
      if (seen.has(path)) continue
      seen.add(path)
      items.push({
        id: `nav:${path}`,
        label: navItem.label,
        path,
        icon: navItem.icon,
        type: 'nav',
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
  const extras: PaletteItem[] = [
    { id: 'nav:/', label: 'Dashboard', path: '/', icon: 'LayoutDashboard', type: 'nav', section: 'BUILD' },
    { id: 'nav:/pipelines', label: 'Pipelines', path: '/pipelines', icon: 'GitFork', type: 'nav', section: 'BUILD' },
    { id: 'nav:/library', label: 'Library', path: '/library', icon: 'BookOpen', type: 'nav', section: 'BUILD' },
    { id: 'nav:/runs', label: 'Runs', path: '/runs', icon: 'CirclePlay', type: 'nav', section: 'BUILD' },
    { id: 'nav:/evals', label: 'Evals', path: '/evals', icon: 'CheckSquare', type: 'nav', section: 'MONITOR' },
    { id: 'nav:/schemas', label: 'Schemas', path: '/schemas', icon: 'Database', type: 'nav', section: 'CONFIGURE' },
    { id: 'nav:/admin/connectors', label: 'Connectors', path: '/admin/connectors', icon: 'Plug', type: 'nav', section: 'ADMIN' },
    { id: 'nav:/settings', label: 'Settings', path: '/settings', icon: 'Settings', type: 'nav', section: 'ADMIN' },
    { id: 'nav:/admin/org', label: 'Organization', path: '/admin/org', icon: 'Building', type: 'nav', section: 'ADMIN' },
    { id: 'nav:/admin/system', label: 'System', path: '/admin/system', icon: 'Settings', type: 'nav', section: 'ADMIN' },
    { id: 'nav:/admin/costs', label: 'Cost Management', path: '/admin/costs', icon: 'DollarSign', type: 'nav', section: 'ADMIN' },
  ]
  const rawItems = getNavGroups().flatMap((g) => g.items)
  for (const extra of extras) {
    if (seen.has(extra.path)) continue
    const hasNavItem = rawItems.some((i) => i.to === extra.path)
    if (hasNavItem && !visiblePaths.has(extra.path)) continue
    seen.add(extra.path)
    items.push(extra)
  }
  return items
})

// Phase 2: user-data results (recents, pipelines, runs, schemas, connectors).
const { results: userResults, loading, error } = useCommandSearch(query, isOpen)

const filteredNavItems = computed<PaletteItem[]>(() => {
  const q = query.value.toLowerCase().trim()
  if (!q) return navItems.value
  return navItems.value.filter(
    item =>
      item.label.toLowerCase().includes(q) ||
      (item.section?.toLowerCase().includes(q) ?? false),
  )
})

const allItems = computed<PaletteItem[]>(() => {
  const seen = new Set<string>()
  const combined: PaletteItem[] = []
  // Ordering: recents (jump-back is the first thing you want on open), then
  // the navigation index (a page query should keep opening a page first, as it
  // did in Phase 1), then the remaining user-data sources.
  const recents = userResults.value.filter((item) => item.type === 'recent')
  const otherResults = userResults.value.filter((item) => item.type !== 'recent')
  for (const item of [...recents, ...filteredNavItems.value, ...otherResults]) {
    // De-duplicate by destination so a page reachable both as a nav item and a
    // recent visit is never listed twice.
    const key = item.path
    if (seen.has(key)) continue
    seen.add(key)
    combined.push(item)
    if (combined.length >= MAX_RESULTS) break
  }
  return combined
})

const activeDescendantId = computed(() =>
  allItems.value.length > 0 ? `cmdk-option-${selectedIndex.value}` : undefined,
)

const statusText = computed(() => {
  if (loading.value) return t('components.CommandPalette.searching')
  // Errors are announced by the role="alert" region instead — one live region
  // per event, so an error is never read twice.
  if (error.value) return ''
  return t(
    'components.CommandPalette.results_count',
    { count: allItems.value.length },
    allItems.value.length,
  )
})

function sectionLabel(item: PaletteItem): string {
  if (item.sectionKey) return t(item.sectionKey)
  return item.section ?? ''
}

watch(selectedIndex, (idx) => {
  nextTick(() => {
    const container = document.querySelector('[data-cmdk-container]')
    if (!container) return
    const buttons = container.querySelectorAll('button')
    buttons[idx]?.scrollIntoView({ block: 'nearest' })
  })
})

watch(allItems, () => {
  const count = allItems.value.length
  if (count === 0) {
    selectedIndex.value = 0
  } else if (selectedIndex.value > count - 1) {
    selectedIndex.value = count - 1
  }
})

/**
 * Resolve a human label + icon for the current route so it can be recorded as
 * a recent visit. Prefers a matching nav item (translated label + real icon),
 * then the route's breadcrumb meta, then the route name. Returns null when
 * nothing usable exists, so junk entries never enter the list.
 */
function resolveRecentVisit(): { label: string; icon: string } | null {
  const path = route.path
  const navItem = navItems.value.find((item) => item.path === path)
  if (navItem) return { label: navItem.label, icon: navItem.icon }
  const meta = route.meta as { breadcrumb?: unknown } | undefined
  if (typeof meta?.breadcrumb === 'string' && meta.breadcrumb) {
    return { label: meta.breadcrumb, icon: 'File' }
  }
  if (typeof route.name === 'string' && route.name) {
    return { label: route.name.replace(/-/g, ' '), icon: 'File' }
  }
  return null
}

watch(
  () => route.fullPath,
  (fullPath) => {
    const resolved = resolveRecentVisit()
    if (resolved) recordRecentVisit({ path: fullPath, label: resolved.label, icon: resolved.icon })
  },
  { immediate: true },
)

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
  const items = allItems.value
  if (items.length === 0) return
  const item = items[selectedIndex.value]
  if (item) goTo(item.path)
}

function goTo(path: string) {
  close()
  router.push(path)
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && isOpen.value) {
    close()
    return
  }
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
