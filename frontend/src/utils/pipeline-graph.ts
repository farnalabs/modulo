/** Shared pipeline-graph helpers used by both the editor view and the typed
 * graph projection adapter (FAR-2475).
 *
 * Keeping these here means the canvas projection and the editor agree on what a
 * declared capability scope looks like instead of each carrying its own copy.
 */

/** Minimal shape of a node-level capability scope. */
export interface CapabilityScopeLike {
  allowed_connectors?: string[] | null
  allowed_tools?: string[] | null
  context_scope?: string[] | null
}

/** Whether a node declares a capability scope (displayed as a badge). */
export function doesNodeHaveCapabilityScope(
  node: { capability_scope?: CapabilityScopeLike | null } | null | undefined
): boolean {
  const scope = node?.capability_scope
  if (!scope || typeof scope !== 'object') return false
  return [scope.allowed_connectors, scope.allowed_tools, scope.context_scope].some(
    (arr) => Array.isArray(arr) && arr.length > 0
  )
}
