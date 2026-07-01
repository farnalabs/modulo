import type { EventBusEvent } from '@/types/events'

const registry = new Map<string, Set<(event: EventBusEvent) => void>>()

export function registerHandler(resourceType: string, handler: (event: EventBusEvent) => void): () => void {
  if (!registry.has(resourceType)) registry.set(resourceType, new Set())
  registry.get(resourceType)!.add(handler)
  return () => {
    const handlers = registry.get(resourceType)
    if (handlers) {
      handlers.delete(handler)
      if (handlers.size === 0) registry.delete(resourceType)
    }
  }
}

export function getHandlers(resourceType: string): Set<(event: EventBusEvent) => void> {
  return registry.get(resourceType) ?? new Set()
}

export function clearAllRegistrations(): void {
  registry.clear()
}
