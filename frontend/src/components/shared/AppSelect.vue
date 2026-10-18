<template>
  <!--
    Wrap the PrimeVue Select in a <label> so the rendered <input> is
    statically associated with a label.  PrimeVue's Select renders an
    <input> internally; without an explicit label association SonarCloud's
    Web:InputWithoutLabelCheck flags it as an unlabelled field (this is the
    reliability-rating gate failure on PR #596).  The wrapper itself never
    renders visible label text, so consumer layout is unchanged - it only
    satisfies the accessibility/static-analysis requirement.  See
    AgentRunnerBindings.vue for the same pattern.
  -->
  <label ref="rootEl">
    <Select
      v-bind="$attrs"
      :options="options"
      :option-label="optionLabel"
      :option-value="optionValue"
      :model-value="modelValue"
      :placeholder="placeholder"
      :append-to="resolvedAppendTo"
      :aria-label="resolvedAriaLabel"
      :pt="mergedPt"
      @update:model-value="emit('update:modelValue', $event)"
      @blur="emit('blur', $event)"
      @focus="emit('focus', $event)"
      @change="emit('change', $event)"
      @before-show="emit('before-show')"
      @before-hide="emit('before-hide')"
      @show="emit('show')"
      @hide="emit('hide')"
      @filter="emit('filter', $event)"
    >
      <template v-for="(_, name) in $slots" #[name]="slotData">
        <slot :name="name" v-bind="slotData ?? {}" />
      </template>
    </Select>
  </label>
</template>

<script setup lang="ts">
/**
 * AppSelect - thin wrapper around PrimeVue Select that:
 *   - resolves the overlay's `appendTo` as: an explicit `append-to` prop →
 *     `'body'` when the Select sits inside any clipping ancestor → `'self'`.
 *     `'self'` keeps the overlay inside the Select's own `position: relative`
 *     wrapper instead of being portalled to `<body>` (FAR-851: filter dropdowns
 *     detaching to the viewport origin); `'body'` escapes a clipping ancestor,
 *     which otherwise clips the overlay and hides or covers its lower options
 *     (FAR-1908 dialogs, FAR-1920 the general case — Drawer, sidebar, custom
 *     `overflow` box);
 *   - wraps the inner Select in a `<label>` and binds an explicit `aria-label`
 *     so the rendered `<input>` is never flagged by SonarCloud's
 *     `Web:InputWithoutLabelCheck`;
 *   - forwards all props, events, and slots to PrimeVue's Select.
 *
 * All Select instances across the app should use this wrapper instead of
 * importing `primevue/select` directly (enforced by appselect-guard.spec.ts).
 *
 * NOTE: the clipping ancestor is detected from the rendered DOM rather than
 * declared by the host. A host-provided flag can only ever be opt-in: a new
 * dialog/drawer/panel that forgets it silently reintroduces the clipping bug
 * (FAR-1908 review). Detection is structural and container-agnostic, so every
 * Select inside every clipping ancestor is safe without the host doing
 * anything.
 *
 * NOTE: `appendTo` is a declared prop with NO default, so "consumer did not
 * pass one" is distinguishable from an explicit value. Consumers that pass a
 * non-empty `append-to` keep control; an empty string is treated as "not
 * given" (passing `append-to=""` to PrimeVue is meaningless and would bypass
 * the derived default). A hardcoded `append-to="self"` after `v-bind="$attrs"`
 * would silently override the consumer's value. Declaring it (rather than
 * reading it out of `$attrs`) is required because Vue keys fallthrough attrs by
 * their original name, so `$attrs.appendTo` is undefined for a kebab-case
 * `append-to`.
 *
 * NOTE: events are declared in `defineEmits` for consumer type-safety AND
 * re-emitted to the inner PrimeVue `Select` below. Declaring an event in
 * `defineEmits` consumes the parent's `onXxx` listener and removes it from
 * `$attrs`, so `v-bind="$attrs"` alone would NOT forward it - the inner
 * `Select` would never propagate `update:modelValue` back to the parent and
 * `v-model` would silently break. Re-emitting here restores the passthrough.
 */
import { computed, onMounted, ref, useAttrs } from 'vue'
import Select from 'primevue/select'

// FAR-1920: the append-target decision is "does this Select sit inside a
// clipping ancestor", not "is it inside a dialog". Any ancestor whose computed
// overflow is not `visible` clips an in-place overlay: the overlay is painted
// outside the ancestor's box, so its lower options are hidden or covered by a
// sibling that paints over them, and a click where an option is drawn lands on
// the wrong element. That condition is a property of the rendered DOM, not of a
// container class, so the predicate is general — a new container type (Drawer,
// sidebar, custom scroll box) needs no new special case. `.p-dialog-content`
// (FAR-1908) is covered by the same rule and its class-specific branch is
// retired.
const CLIPPING_OVERFLOW = new Set(['hidden', 'clip', 'auto', 'scroll'])

// PrimeVue declares each scrollable overlay-content box as `p-<component>-content`
// and styles it with `overflow`, e.g. `.p-dialog-content`, `.p-drawer-content`,
// `.p-sidebar-content`. The computed overflow above is the authoritative signal
// in a browser, but an environment without the stylesheet loaded (jsdom unit
// tests stub CSS) computes `overflow: visible` for these boxes, so the declared
// contract is the fallback. The shape — any `p-*-content` box, not a hardcoded
// list of container types — is what keeps a new PrimeVue container covered
// without a new special case.
const PRIMEVUE_CONTENT_BOX = /^p-[a-z0-9-]+-content$/

/**
 * Does a computed style clip its overflow on either axis?
 *
 * A browser reports the two axes as longhands (`overflow-x`/`overflow-y`); in
 * an environment that does not expand the `overflow` shorthand the longhands
 * stay `visible` for `style="overflow: auto"`, so the shorthand is expanded
 * here too. Reading the shorthand is only a fallback — the longhands are
 * authoritative where present.
 */
function clipsOverflow(style: CSSStyleDeclaration): boolean {
  if (CLIPPING_OVERFLOW.has(style.overflowX) || CLIPPING_OVERFLOW.has(style.overflowY)) {
    return true
  }
  const shorthand = style.overflow.trim()
  if (!shorthand) return false
  return shorthand.split(/\s+/).some((value) => CLIPPING_OVERFLOW.has(value))
}

/**
 * Is a single ancestor a clipping container?
 *
 * The live computed overflow is the primary signal (it also covers plain
 * elements that clip via a utility class or inline style). The PrimeVue
 * content-box class shape is the fallback for a stylesheet-less environment.
 */
function isClippingContainer(node: HTMLElement): boolean {
  if (clipsOverflow(getComputedStyle(node))) return true
  for (const token of Array.from(node.classList)) {
    if (PRIMEVUE_CONTENT_BOX.test(token)) return true
  }
  return false
}

/**
 * Walk from the element up through its ancestors (stopping at `<body>`/`<html>`,
 * which are not clipping containers) and report whether any of them clips its
 * overflow.
 *
 * `getComputedStyle` reads the used value in a browser, so a real dialog's
 * `.p-dialog-content` (`overflow-y: auto`) is detected without layout being
 * present. Each ancestor is checked on both axes because a container may clip
 * one axis only, and the whole chain is walked because a Drawer/sidebar wraps
 * the Select several levels deep.
 */
function hasClippingAncestor(el: HTMLElement | null): boolean {
  let node: HTMLElement | null = el
  while (node) {
    if (node !== document.body && node !== document.documentElement && isClippingContainer(node)) {
      return true
    }
    node = node.parentElement
  }
  return false
}

// Keep $attrs (e.g. data-testid) on the inner PrimeVue <Select> only.
// With default inheritance the wrapper <label> would also receive them,
// producing duplicate attributes (e.g. two elements sharing the same
// data-testid) and breaking strict-mode selectors.
defineOptions({ inheritAttrs: false })

const emit = defineEmits<{
  (e: 'update:modelValue', value: unknown): void
  (e: 'blur', event: Event): void
  (e: 'focus', event: Event): void
  (e: 'change', event: unknown): void
  (e: 'before-show'): void
  (e: 'before-hide'): void
  (e: 'show'): void
  (e: 'hide'): void
  (e: 'filter', event: unknown): void
}>()

const props = withDefaults(
  defineProps<{
    options?: unknown[]
    optionLabel?: string
    optionValue?: string
    modelValue?: unknown
    placeholder?: string
    appendTo?: string
    /**
     * Accessible label for the underlying PrimeVue `Select` (which renders an
     * `<input>` internally). Falls back to an `aria-label` passed through
     * `$attrs`, then to a generic default.
     */
    label?: string
  }>(),
  {
    label: '',
  },
)

const attrs = useAttrs()

const rootEl = ref<HTMLElement | null>(null)
const isInClippingAncestor = ref(false)

// Detect the clipping ancestor from the rendered DOM (measurement is only
// possible after mount) so the safe append target is structural rather than
// opted into by the host — a new dialog/drawer/panel that forgets a flag would
// silently reintroduce the clipping bug (FAR-1908 review).
onMounted(() => {
  isInClippingAncestor.value = hasClippingAncestor(rootEl.value)
})

// Precedence: explicit prop → clipping-ancestor-derived → FAR-851 'self'. An
// empty-string prop is treated as absent (PrimeVue cannot use '' as an append
// target, and passing it through would silently bypass the derived default).
const resolvedAppendTo = computed(() => {
  if (props.appendTo) return props.appendTo
  return isInClippingAncestor.value ? 'body' : 'self'
})

// Consumers usually pass `aria-label` through `$attrs`; surface it explicitly
// so the label association is statically visible and the meaningful
// per-instance label (e.g. "Level") is preserved.
const resolvedAriaLabel = computed(
  () => props.label || (attrs['aria-label'] as string | undefined) || 'Select',
)

// FAR-1913: axe's `scrollable-region-focusable` flags PrimeVue's
// `.p-select-list-container` (the overlay's `overflow: auto` scroll box) because
// it has no keyboard entry point. Making the container focusable is the fix. It
// is applied here, in the single Select wrapper, so every Select inherits it.
//
// `pt` may also arrive from a consumer through `$attrs` (e.g. FilterBar's
// per-option spacing, the environment-profile form's `aria-describedby`), so
// this MERGES with the consumer's `pt` rather than replacing it - an explicit
// `:pt` after `v-bind="$attrs"` would silently drop the consumer's config.
type PtSection = Record<string, unknown> | ((opts: unknown) => Record<string, unknown>)

function withFocusableListContainer(existing: unknown): PtSection {
  if (typeof existing === 'function') {
    const existingFn = existing as (opts: unknown) => Record<string, unknown>
    return (opts) => ({ ...existingFn(opts), tabindex: '0' })
  }
  return { ...(existing as Record<string, unknown> | undefined), tabindex: '0' }
}

const mergedPt = computed(() => {
  const consumerPt = attrs.pt as Record<string, unknown> | undefined
  return {
    ...consumerPt,
    listContainer: withFocusableListContainer(consumerPt?.listContainer),
  }
})
</script>
