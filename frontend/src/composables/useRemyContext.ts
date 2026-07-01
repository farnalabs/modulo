import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import type { PageContext } from '@/types/remy'

export function useRemyContext() {
  const route = useRoute()
  const pageContext = ref<PageContext>({
    route: '',
    params: {},
    entities: [],
  })

  function extractEntities(routeName: string, params: Record<string, string>): string[] {
    const entities: string[] = []
    if (params.id) entities.push(`run:${params.id}`)
    if (params.teamId) entities.push(`team:${params.teamId}`)
    if (params.pipelineId) entities.push(`pipeline:${params.pipelineId}`)
    return entities
  }

  watch(
    () => [route.name, route.params, route.path] as const,
    ([name, params]) => {
      const resolved: Record<string, string> = {}
      for (const [k, v] of Object.entries(params)) {
        resolved[k] = typeof v === 'string' ? v : Array.isArray(v) ? v[0] ?? '' : ''
      }
      pageContext.value = {
        route: (name as string) ?? route.path,
        params: resolved,
        entities: extractEntities(name as string, resolved),
      }
    },
    { immediate: true },
  )

  return { pageContext }
}
