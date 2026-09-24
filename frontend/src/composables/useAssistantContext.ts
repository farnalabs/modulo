import { ref, readonly, watch } from 'vue'
import { useRoute } from 'vue-router'
import type { PageContext } from '@/types/assistant'

function extractEntities(params: Record<string, string>): string[] {
  const entities: string[] = []
  if (params.id) entities.push(`run:${params.id}`)
  if (params.teamId) entities.push(`team:${params.teamId}`)
  if (params.pipelineId) entities.push(`pipeline:${params.pipelineId}`)
  return entities
}

export function useAssistantContext() {
  const route = useRoute()
  const pageContext = ref<PageContext>({
    route: '',
    params: {},
    entities: [],
  })

  watch(
    () => [route.name, route.params, route.path] as const,
    ([name, params]) => {
      const resolved: Record<string, string> = {}
      for (const [k, v] of Object.entries(params)) {
        if (typeof v === 'string') {
          resolved[k] = v
        } else if (Array.isArray(v)) {
          resolved[k] = v[0] ?? ''
        } else {
          resolved[k] = ''
        }
      }
      pageContext.value = {
        route: (name as string) ?? route.path,
        params: resolved,
        entities: extractEntities(resolved),
      }
    },
    { immediate: true },
  )

  return { pageContext: readonly(pageContext) }
}
