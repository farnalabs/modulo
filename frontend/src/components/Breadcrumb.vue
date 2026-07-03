<template>
  <nav v-if="segments.length > 0" aria-label="Breadcrumb" class="breadcrumb">
    <template v-for="(segment, index) in segments" :key="index">
      <router-link
        v-if="index < segments.length - 1"
        :to="segment.path"
        class="breadcrumb-link"
      >
        {{ segment.label }}
      </router-link>
      <span
        v-else
        aria-current="page"
        class="breadcrumb-current"
      >
        {{ segment.label }}
      </span>
      <span v-if="index < segments.length - 1" class="separator">&gt;</span>
    </template>
  </nav>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { defineOptions } from 'vue'
import manifest from '@/manifest.yaml'

defineOptions({ name: 'AppBreadcrumb' })

interface BreadcrumbSegment {
  path: string
  label: string
}

interface ManifestEntry {
  name: string
  breadcrumb: string
  parent: string | null
  path: string
}

const route = useRoute()
const router = useRouter()

const rawRoutes = (manifest as { routes?: Record<string, Omit<ManifestEntry, 'path'>> })?.routes ?? {}
const manifestByName = new Map<string, ManifestEntry>()
for (const [path, entry] of Object.entries(rawRoutes)) {
  if (entry.name) {
    manifestByName.set(entry.name, { ...entry, path })
  }
}

const segments = computed<BreadcrumbSegment[]>(() => {
  const meta = route.meta as Record<string, unknown> | undefined
  if (!meta?.breadcrumb) return []

  const chain: BreadcrumbSegment[] = []
  let currentName = route.name as string | undefined
  const visited = new Set<string>()

  while (currentName && !visited.has(currentName)) {
    visited.add(currentName)
    const isCurrent = currentName === route.name
    const manifestEntry = manifestByName.get(currentName)
    const resolved = router.resolve({ name: currentName })

    if (manifestEntry) {
      chain.unshift({
        path: isCurrent ? route.path : manifestEntry.path,
        label: manifestEntry.breadcrumb || currentName,
      })
      if (manifestEntry.parent) {
        const parentEntry = rawRoutes[manifestEntry.parent]
        currentName = parentEntry?.name ?? undefined
      } else {
        currentName = undefined
      }
    } else {
      const resolvedMeta = resolved.meta as Record<string, unknown> | undefined
      chain.unshift({
        path: isCurrent ? route.path : resolved.path,
        label: (resolvedMeta?.breadcrumb as string) || currentName,
      })
      currentName = resolvedMeta?.parent as string | undefined
    }
  }

  return chain
})
</script>

<style scoped>
.breadcrumb {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.875rem;
}

.breadcrumb-link {
  color: hsl(var(--muted-foreground));
  text-decoration: none;
}

.breadcrumb-link:hover {
  color: hsl(var(--foreground));
  text-decoration: underline;
}

.separator {
  color: hsl(var(--muted-foreground));
  user-select: none;
}

.breadcrumb-current {
  color: hsl(var(--foreground));
  font-weight: 500;
}
</style>
