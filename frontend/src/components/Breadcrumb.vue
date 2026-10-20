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

function buildManifestMaps() {
  const byName = new Map<string, ManifestEntry>()
  const byPath = new Map<string, ManifestEntry>()
  try {
    const rawRoutes = (manifest as { routes?: Record<string, Omit<ManifestEntry, 'path'>> })?.routes ?? {}
    for (const [path, entry] of Object.entries(rawRoutes)) {
      if (entry.name) {
        const manifestEntry = { ...entry, path }
        byName.set(entry.name, manifestEntry)
        byPath.set(path, manifestEntry)
      }
    }
  } catch {
    // A missing or malformed build manifest leaves the breadcrumb map empty.
  }
  return { byName, byPath }
}

const { byName: manifestByName, byPath: manifestByPath } = buildManifestMaps()

interface ResolvedSegment {
  segment: BreadcrumbSegment
  nextName: string | undefined
}

// Resolve one breadcrumb hop: the manifest is authoritative when it knows the
// route, otherwise fall back to the router's own meta chain.
function resolveSegment(name: string): ResolvedSegment {
  const isCurrent = name === route.name
  const manifestEntry = manifestByName.get(name)

  if (manifestEntry) {
    const parentName = manifestEntry.parent
      ? manifestByPath.get(manifestEntry.parent)?.name
      : undefined
    return {
      segment: {
        path: isCurrent ? route.path : manifestEntry.path,
        label: manifestEntry.breadcrumb || name,
      },
      nextName: parentName,
    }
  }

  const resolved = router.resolve({ name })
  const resolvedMeta = resolved.meta as Record<string, unknown> | undefined
  return {
    segment: {
      path: isCurrent ? route.path : resolved.path,
      label: (resolvedMeta?.breadcrumb as string) || name,
    },
    nextName: resolvedMeta?.parent as string | undefined,
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
    const { segment, nextName } = resolveSegment(currentName)
    chain.unshift(segment)
    currentName = nextName
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
  margin-bottom: 4px;
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
