<template>
  <!-- The ONE folder-list failure surface. Every view that loads folders
       renders this component instead of a bespoke ErrorAlert, so the contract
       is structural rather than repeated four times.

       Single live region: exactly one folder-error live region must be
       visible/announced per breakpoint. `gated` adds `md:hidden` ONLY for a
       surface that coexists with FolderTree's `hidden md:flex` sidebar — the
       sidebar owns >=md and the gated surface owns <md, so the pair never
       announces twice and never falls silent. A surface with NO FolderTree
       must NOT be gated: a gate there would hide the failure at >=md, which is
       exactly where it occurs.

       Deliberately NOT dismissible: this component exposes no dismiss prop, so
       no call site can make it dismissible. Dismissing the folder-list failure
       would strand the folder list empty (or silently wrong) with no retry
       until a full reload; retry is the only recovery affordance.

       The root is <ErrorAlert> directly — no wrapper element — so the `class`
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
// dismiss prop exists by design — see the template comment.
withDefaults(defineProps<{
  message: string
  onRetry: () => void
  gated?: boolean
  compact?: boolean
}>(), { gated: false, compact: false })
</script>
