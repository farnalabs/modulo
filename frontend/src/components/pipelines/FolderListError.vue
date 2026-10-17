<template>
  <!-- The ONE folder-list failure surface. Every view that loads folders
       renders this component instead of a bespoke ErrorAlert, so the contract
       is written once rather than repeated four times.

       Single live region: exactly one folder-error live region is announced
       per breakpoint. `gated` adds `md:hidden` ONLY for a surface that
       coexists with FolderTree's `hidden md:flex` sidebar. The gated surface
       owns <md precisely because the sidebar is `hidden md:flex`; at >=md the
       sidebar IS the folder surface, so if it loaded successfully there is
       nothing to announce and the silence is correct, not a gap. The pair
       therefore covers both breakpoints in the failure mode that matters:
       both fetches failing together, which is what a bad response shape
       produces. The two fetches are independent calls to the same endpoint,
       so a view's own fetch failing while FolderTree's fetch of that endpoint
       succeeds leaves the >=md sidebar populated and the `md:hidden` alert
       hidden. A surface with NO FolderTree must NOT be gated: a gate there
       would hide the failure at >=md, which is exactly where it occurs.

       Deliberately NOT dismissible: the component declares no dismiss prop,
       so there is no supported way to dismiss this surface, and none of the
       four call sites passes one. Non-dismissibility is guarded by spec
       assertions at the surfaces that render it (the list views' specs assert
       the dismiss affordance never renders); it is NOT enforced structurally,
       because ErrorAlert declares `onDismiss`/`dismissLabel` props, so a
       caller could re-enable dismissal by passing `on-dismiss`/`dismiss-label`
       as attrs, which fall through. Dismissing the folder-list failure would
       strand the folder list empty (or silently wrong) with no retry until a
       full reload; retry is the only recovery affordance.

       The root is <ErrorAlert> directly - no wrapper element - so the `class`
       and `data-testid` a call site passes fall through (FolderListError ->
       ErrorAlert -> its root <div>) and the alert renders as a single node. -->
  <ErrorAlert
    :message="message"
    :on-retry="onRetry"
    :compact="compact"
    role="alert"
    :class="{ 'md:hidden': gated }"
  />
</template>

<script setup lang="ts">
import ErrorAlert from '../shared/ErrorAlert.vue'

// Mirrors ErrorAlert's prop idiom (a leading `on` for the callback). No
// dismiss prop exists by design - see the template comment.
withDefaults(defineProps<{
  message: string
  onRetry: () => void
  gated?: boolean
  compact?: boolean
}>(), { gated: false, compact: false })
</script>
