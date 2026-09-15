<template>
  <Select
    v-bind="$attrs"
    :options="options"
    :option-label="optionLabel"
    :option-value="optionValue"
    :model-value="modelValue"
    :placeholder="placeholder"
    append-to="self"
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
 */
import Select from 'primevue/select'

defineOptions({ inheritAttrs: false })

defineProps<{
  options?: unknown[]
  optionLabel?: string
  optionValue?: string
  modelValue?: unknown
  placeholder?: string
}>()

defineEmits<{
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
</script>
