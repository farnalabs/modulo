<template>
  <ErrorAlert
    v-bind="forwardedAttrs"
    :message="message"
    :on-retry="onRetry"
    :compact="compact"
    role="alert"
    :class="{ 'md:hidden': gated }"
  />
</template>

<script setup lang="ts">
/*
 * The ONE folder-list failure surface. Every view that loads folders
 * renders this component instead of a bespoke ErrorAlert, so the contract
 * is written once rather than repeated four times.
 *
 * Single live region: exactly one folder-error live region is announced
 * per breakpoint. `gated` adds `md:hidden` ONLY for a surface that
 * coexists with FolderTree's `hidden md:flex` sidebar. The gated surface
 * owns <md precisely because the sidebar is `hidden md:flex`; at >=md the
 * sidebar IS the folder surface, so if it loaded successfully there is
 * nothing to announce and the silence is correct, not a gap. The pair
 * therefore covers both breakpoints in the failure mode that matters:
 * both fetches failing together, which is what a bad response shape
 * produces. The two fetches are independent calls to the same endpoint,
 * so a view's own fetch failing while FolderTree's fetch of that endpoint
 * succeeds leaves the >=md sidebar populated and the `md:hidden` alert
 * hidden. A surface with NO FolderTree must NOT be gated: a gate there
 * would hide the failure at >=md, which is exactly where it occurs.
 *
 * Deliberately NOT dismissible: the component declares no dismiss prop,
 * and non-dismissibility is enforced structurally - the attrs that would
 * re-enable the dismiss button (`on-dismiss`/`dismiss-label`/
 * `dismiss-icon`, in either their kebab or camel spelling) never reach
 * ErrorAlert, because automatic fallthrough is disabled and those attrs
 * are filtered out of the forwarded set. Dismissing the folder-list
 * failure would strand the folder list empty (or silently wrong) with no
 * retry until a full reload; retry is the only recovery affordance.
 *
 * That same filtered set also refuses `retryable`, because retry is this
 * surface's only recovery affordance: a call site may not switch it off,
 * so `retryable` stays at ErrorAlert's default (true), and the button
 * renders whenever the required `onRetry` callback is supplied - which
 * every call site does. A refused attr is reported in development,
 * dropped without a report in production. Every other attr is still
 * forwarded.
 *
 * The root is <ErrorAlert> directly - no wrapper element - so the `class`
 * and `data-testid` a call site passes are forwarded (FolderListError ->
 * ErrorAlert -> its root <div>) and the alert renders as a single node.
 */
import { computed, useAttrs, watchEffect } from 'vue'
import ErrorAlert from '../shared/ErrorAlert.vue'

// Automatic fallthrough is off so a call site can never re-enable ErrorAlert's
// dismiss button or switch off its retry affordance: ErrorAlert declares
// onDismiss/dismissLabel/dismissIcon and retryable, so with fallthrough a
// caller passing on-dismiss or retryable=false would take effect. Unsupported
// attrs are refused and reported in development, dropped without a report in
// production; every other attr is still forwarded.
defineOptions({ inheritAttrs: false })

// Read the fallthrough attrs once, in setup: both computeds below filter the
// same reactive attrs object, so hoisting the single useAttrs() call avoids
// calling it twice and keeps the read where the composition API documents it.
const attrs = useAttrs()

const UNSUPPORTED_ATTRS = new Set(['ondismiss', 'dismisslabel', 'dismissicon', 'retryable'])
const normaliseAttrName = (key: string) => key.replace(/-/g, '').toLowerCase()

const forwardedAttrs = computed(() =>
  Object.fromEntries(
    Object.entries(attrs).filter(
      ([key]) => !UNSUPPORTED_ATTRS.has(normaliseAttrName(key))
    )
  )
)

// A refused attr must not vanish without a trace - a call site that passed one
// believes it configured something. Dev-only so production stays quiet, and
// watchEffect-based so it reports the current attr set rather than firing on
// every render.
const unsupportedAttrNames = computed(() =>
  Object.keys(attrs).filter((key) => UNSUPPORTED_ATTRS.has(normaliseAttrName(key)))
)

// `import.meta.env.DEV` is statically replaced with `false` in a production
// build, so this block and its watcher are eliminated there.
if (import.meta.env.DEV) {
  watchEffect(() => {
    if (unsupportedAttrNames.value.length > 0) {
      console.warn(
        `FolderListError: ignoring unsupported attr(s) on the folder-error surface: ${unsupportedAttrNames.value.join(', ')}. The surface is never dismissible and its retry affordance is always shown.`
      )
    }
  })
}

// Mirrors ErrorAlert's prop idiom (a leading `on` for the callback). No
// dismiss prop exists by design - see the comment above.
withDefaults(defineProps<{
  message: string
  onRetry: () => void
  gated?: boolean
  compact?: boolean
}>(), { gated: false, compact: false })
</script>
