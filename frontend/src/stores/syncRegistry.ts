import type { Ref } from "vue";
import type { EventBusEvent } from "@/types/events";

const registry = new Map<string, Set<(event: EventBusEvent) => void>>();

export function registerHandler(
  resourceType: string,
  handler: (event: EventBusEvent) => void,
): () => void {
  if (!registry.has(resourceType)) registry.set(resourceType, new Set());
  registry.get(resourceType)!.add(handler);
  return () => {
    const handlers = registry.get(resourceType);
    if (handlers) {
      handlers.delete(handler);
      if (handlers.size === 0) registry.delete(resourceType);
    }
  };
}

export function getHandlers(
  resourceType: string,
): Set<(event: EventBusEvent) => void> {
  return new Set(registry.get(resourceType) ?? []);
}

export function clearAllRegistrations(): void {
  registry.clear();
}

/**
 * Register one event handler per [resourceType, handler] pair and wire up HMR
 * disposal so the handlers (and the store's transient sync state) are torn
 * down cleanly on hot reload. Shared by the per-resource stores to keep the
 * registration + HMR-dispose boilerplate in a single place (avoids the
 * duplicated registerHandler/HMR-dispose block that tripped the new-code
 * duplication gate).
 */
export function registerSyncHandlers(
  unsubHandlers: Array<() => void>,
  syncingIds: Ref<Set<string>>,
  handlers: Array<[string, (event: EventBusEvent) => void]>,
): void {
  for (const [resourceType, handler] of handlers) {
    unsubHandlers.push(registerHandler(resourceType, handler));
  }
  if (import.meta.hot) {
    import.meta.hot.dispose(() => {
      disposeSyncHandlers(unsubHandlers, syncingIds);
    });
  }
}

export function disposeSyncHandlers(
  unsubHandlers: Array<() => void>,
  syncingIds: Ref<Set<string>>,
): void {
  for (const unsub of unsubHandlers) unsub();
  unsubHandlers.length = 0;
  syncingIds.value.clear();
}

if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    clearAllRegistrations();
  });
}
