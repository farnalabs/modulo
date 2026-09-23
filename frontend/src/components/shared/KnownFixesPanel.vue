<script setup lang="ts">
import { computed } from 'vue'

/**
 * One curated known-fix entry matched server-side against the run's raw
 * error detail (FAR-706). Mirrors the wire shape of
 * `RunResponse.known_fixes`, typed loosely to the generated OpenAPI index
 * signature and normalised before render.
 */
type WireKnownFix = Record<string, unknown>

type RenderableKnownFix = {
  fix_id: string | null
  title: string
  body: string
  link: string | null
}

const props = defineProps<{
  fixes?: WireKnownFix[] | null
}>()

// Fail-safe (FAR-706): display-only — normalise each entry and drop any
// malformed one instead of letting it break the run detail page. Renders
// NOTHING when the list is absent, empty, or entirely malformed (no empty box).
const validFixes = computed<RenderableKnownFix[]>(() => {
  const raw = props.fixes
  if (!Array.isArray(raw)) return []
  const out: RenderableKnownFix[] = []
  for (const fix of raw) {
    if (!fix || typeof fix.title !== 'string' || !fix.title || typeof fix.body !== 'string' || !fix.body) {
      continue
    }
    out.push({
      fix_id: typeof fix.fix_id === 'string' && fix.fix_id ? fix.fix_id : null,
      title: fix.title,
      body: fix.body,
      link: typeof fix.link === 'string' && fix.link ? fix.link : null,
    })
  }
  return out
})
</script>

<template>
  <section
    v-if="validFixes.length > 0"
    data-testid="run-detail-known-fixes"
    aria-live="polite"
    class="mb-4 rounded-lg border border-primary/40 bg-primary/5 p-4"
  >
    <h3 class="mb-2 text-sm font-semibold text-foreground">
      {{ $t('views.RunDetailView.known_fix_heading') }}
    </h3>
    <article
      v-for="(fix, index) in validFixes"
      :key="fix.fix_id || index"
      :data-testid="`run-detail-known-fix-${fix.fix_id || index}`"
      class="mb-3 last:mb-0"
    >
      <p class="mb-1 text-sm font-medium text-foreground">{{ fix.title }}</p>
      <p class="mb-1 whitespace-pre-wrap text-xs text-muted-foreground">{{ fix.body }}</p>
      <a
        v-if="fix.link"
        :href="fix.link"
        target="_blank"
        rel="noopener noreferrer"
        class="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
      >
        {{ $t('views.RunDetailView.known_fix_reference') }}
      </a>
    </article>
  </section>
</template>
