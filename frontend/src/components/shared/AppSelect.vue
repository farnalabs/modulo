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
 *     `'body'` when the Select is rendered inside a dialog's scrolling content
 *     box → `'self'`. `'self'` keeps the overlay inside the Select's own
 *     `position: relative` wrapper instead of being portalled to `<body>`
 *     (FAR-851: filter dropdowns detaching to the viewport origin); `'body'`
 *     escapes a dialog's `.p-dialog-content` scroll container, which otherwise
 *     clips the overlay and hides its lower options behind `.p-dialog-footer`
 *     (FAR-1908);
 *   - wraps the inner Select in a `<label>` and binds an explicit `aria-label`
 *     so the rendered `<input>` is never flagged by SonarCloud's
 *     `Web:InputWithoutLabelCheck`;
 *   - forwards all props, events, and slots to PrimeVue's Select.
 *
 * All Select instances across the app should use this wrapper instead of
 * importing `primevue/select` directly (enforced by appselect-guard.spec.ts).
 *
 * NOTE: the dialog case is detected from the rendered DOM (`.p-dialog-content`
 * ancestor) rather than declared by the host. A host-provided flag can only ever
 * be opt-in: a new dialog that forgets it silently reintroduces the clipping
 * bug (FAR-1908 review). Detection is structural, so every Select inside every
 * dialog — including raw `<Dialog>` hosts and PrimeVue's own content box — is
 * safe without the host doing anything.
 *
 * NOTE: `appendTo` is a declared prop with NO default, so "consumer did not
 * pass one" is distinguishable from an explicit value. Consumers that pass a
 * non-empty `append-to` keep control; an empty string is treated as "not
 * given" (passing `append-to=""` to PrimeVue is meaningless and would bypass
 * the dialog default). A hardcoded `append-to="self"` after `v-bind="$attrs"`
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
const isInDialogContent = ref(false)

// FAR-1908: a dialog renders its body in `.p-dialog-content`, which is a scroll
// container (`overflow-y: auto`) that clips an in-place overlay and leaves its
// lower options covered by the dialog footer. Detect that container from the
// rendered DOM so the safe append target is structural rather than opt-in.
onMounted(() => {
  isInDialogContent.value = rootEl.value?.closest('.p-dialog-content') != null
})

// Precedence: explicit prop → dialog-derived → FAR-851 'self'. An empty-string
// prop is treated as absent (PrimeVue cannot use '' as an append target, and
// passing it through would silently bypass the dialog default).
const resolvedAppendTo = computed(() => {
  if (props.appendTo) return props.appendTo
  return isInDialogContent.value ? 'body' : 'self'
})

// Consumers usually pass `aria-label` through `$attrs`; surface it explicitly
// so the label association is statically visible and the meaningful
// per-instance label (e.g. "Level") is preserved.
const resolvedAriaLabel = computed(
  () => props.label || (attrs['aria-label'] as string | undefined) || 'Select',
)
</script>
