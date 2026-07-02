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
defineOptions({ name: 'AppBreadcrumb' })

interface BreadcrumbSegment {
  path: string
  label: string
}

const route = useRoute()
const router = useRouter()

const segments = computed<BreadcrumbSegment[]>(() => {
  const meta = route.meta as Record<string, unknown> | undefined
  if (!meta?.breadcrumb) return []

  const chain: { name: string; label: string }[] = []
  chain.unshift({ name: route.name as string, label: meta.breadcrumb as string })

  let parentName = meta.parent as string | undefined
  while (parentName) {
    try {
      const parentRoute = router.resolve({ name: parentName })
      const parentMeta = parentRoute.meta as Record<string, unknown> | undefined
      chain.unshift({
        name: parentName,
        label: (parentMeta?.breadcrumb as string) || parentName,
      })
      parentName = parentMeta?.parent as string | undefined
    } catch {
      break
    }
  }

  return chain.map((item, index) => ({
    path: router.resolve({ name: item.name }).path,
    label: item.label,
  }))
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
