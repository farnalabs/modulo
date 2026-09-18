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
  <label>
    <Select
      v-bind="$attrs"
      :options="options"
      :option-label="optionLabel"
      :option-value="optionValue"
      :model-value="modelValue"
      :placeholder="placeholder"
      :append-to="appendTo"
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
 *   - defaults `appendTo` to `'self'` so the overlay is rendered inside the
 *     Select's own `position: relative` wrapper instead of being portalled to
 *     `<body>` (FAR-851: filter dropdowns detaching to the viewport origin);
 *   - wraps the inner Select in a `<label>` and binds an explicit `aria-label`
 *     so the rendered `<input>` is never flagged by SonarCloud's
 *     `Web:InputWithoutLabelCheck`;
 *   - forwards all props, events, and slots to PrimeVue's Select.
 *
 * All Select instances across the app should use this wrapper instead of
 * importing `primevue/select` directly (enforced by appselect-guard.spec.ts).
 *
 * NOTE: `appendTo` is a declared prop defaulting to `'self'`. Consumers that
 * pass `append-to` keep control; a hardcoded `append-to="self"` after
 * `v-bind="$attrs"` would silently override the consumer's value. Declaring it
 * (rather than reading it out of `$attrs`) is required because Vue keys
 * fallthrough attrs by their original name, so `$attrs.appendTo` is undefined
 * for a kebab-case `append-to`.
 *
 * NOTE: events are declared in `defineEmits` for consumer type-safety AND
 * re-emitted to the inner PrimeVue `Select` below. Declaring an event in
 * `defineEmits` consumes the parent's `onXxx` listener and removes it from
 * `$attrs`, so `v-bind="$attrs"` alone would NOT forward it - the inner
 * `Select` would never propagate `update:modelValue` back to the parent and
 * `v-model` would silently break. Re-emitting here restores the passthrough.
 */
import { computed, useAttrs } from 'vue'
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
    appendTo: 'self',
    label: '',
  },
)

const attrs = useAttrs()

// Consumers usually pass `aria-label` through `$attrs`; surface it explicitly
// so the label association is statically visible and the meaningful
// per-instance label (e.g. "Level") is preserved.
const resolvedAriaLabel = computed(
  () => props.label || (attrs['aria-label'] as string | undefined) || 'Select',
)
</script>
