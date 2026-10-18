/**
 * FAR-53 (Cmd+K Phase 2): debounced orchestration of the command palette's
 * user-data providers.
 *
 * Owns the async lifecycle so the palette component stays presentational:
 * one debounced search per typing pause (never one request per keystroke),
 * aggregated results across all providers, a loading flag, and a non-fatal
 * error flag. A single provider failing degrades to partial results; only a
 * total failure surfaces the error state.
 */
import { onUnmounted, ref, watch, type Ref } from 'vue'
import { PALETTE_PROVIDERS, type PaletteItem, type PaletteProvider } from '../lib/commandPalette/providers'

/** Matches the app's other search boxes (`useDataFetch({ debounce: 300 })`). */
export const COMMAND_SEARCH_DEBOUNCE_MS = 250

export interface CommandSearchOptions {
  debounceMs?: number
  providers?: PaletteProvider[]
}

export interface CommandSearch {
  results: Ref<PaletteItem[]>
  loading: Ref<boolean>
  error: Ref<boolean>
}

export function useCommandSearch(
  query: Ref<string>,
  enabled: Ref<boolean>,
  options: CommandSearchOptions = {},
): CommandSearch {
  const providers = options.providers ?? PALETTE_PROVIDERS
  const debounceMs = options.debounceMs ?? COMMAND_SEARCH_DEBOUNCE_MS

  const results = ref<PaletteItem[]>([])
  const loading = ref(false)
  const error = ref(false)

  let timer: ReturnType<typeof setTimeout> | null = null
  // Monotonic request token: a response belonging to a superseded query is
  // discarded so out-of-order completions can never overwrite the latest.
  let requestId = 0

  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  function providersFor(text: string): PaletteProvider[] {
    const length = text.trim().length
    return providers.filter((provider) => length >= provider.minQueryLength)
  }

  async function run(text: string): Promise<void> {
    const id = ++requestId
    const active = providersFor(text)
    if (active.length === 0) {
      results.value = []
      loading.value = false
      error.value = false
      return
    }

    loading.value = true
    error.value = false
    const settled = await Promise.allSettled(active.map((provider) => provider.search(text)))
    // A newer query has started; this response is stale.
    if (id !== requestId) return

    const merged: PaletteItem[] = []
    let failures = 0
    for (const outcome of settled) {
      if (outcome.status === 'fulfilled') {
        merged.push(...outcome.value)
      } else {
        failures += 1
        console.warn('[command-palette] provider search failed', outcome.reason)
      }
    }
    results.value = merged
    // Surface an error only when a provider failed AND nothing at all came
    // back. A partial result set is still useful, so it must not read as a
    // broken palette — but a failure with no results is a real degradation
    // (the recents provider always succeeds with [], so "all failed" alone
    // would make the error state unreachable).
    error.value = failures > 0 && merged.length === 0
    loading.value = false
  }

  function schedule(text: string): void {
    clearTimer()
    if (!enabled.value) return
    // Invalidate any in-flight request and drop the previous query's results
    // straight away — showing rows that no longer match the current query
    // (for the whole debounce window) is wrong, and it also prevents an
    // older, slower response from repainting stale results.
    requestId += 1
    results.value = []
    error.value = false
    loading.value = true
    timer = setTimeout(() => {
      timer = null
      void run(text)
    }, debounceMs)
  }

  watch(query, (text) => schedule(text))

  watch(enabled, (isOpen) => {
    if (isOpen) {
      // Opening is a deliberate action — search immediately rather than making
      // the user wait out the debounce for the initial (recents) results.
      clearTimer()
      void run(query.value)
    } else {
      clearTimer()
      requestId += 1
      results.value = []
      loading.value = false
      error.value = false
    }
  })

  onUnmounted(clearTimer)

  return { results, loading, error }
}
