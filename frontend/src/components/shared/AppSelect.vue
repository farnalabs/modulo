<template>
  <Select
    v-bind="$attrs"
    :options="options"
    :option-label="optionLabel"
    :option-value="optionValue"
    :model-value="modelValue"
    :placeholder="placeholder"
    :append-to="appendTo ?? 'self'"
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
</template>

<script setup lang="ts">
/**
 * Shared Select wrapper that defaults `appendTo` to `'self'`
 * to prevent dropdowns detaching to the viewport origin (FAR-851, FAR-869).
 *
 * Forwards all props, events, and slots to PrimeVue's Select.
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
 * `$attrs`, so `v-bind="$attrs"` alone would NOT forward it — the inner
 * `Select` would never propagate `update:modelValue` back to the parent and
 * `v-model` would silently break. Re-emitting here restores the passthrough.
 */
import Select from 'primevue/select'

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

defineProps<{
  options?: unknown[]
  optionLabel?: string
  optionValue?: string
  modelValue?: unknown
  placeholder?: string
  appendTo?: string
}>()
</script>
