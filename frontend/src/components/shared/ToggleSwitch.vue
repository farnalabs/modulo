<template>
  <button
    type="button"
    class="inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
    role="switch"
    :aria-checked="checked"
    :aria-label="label"
    :aria-disabled="disabled || undefined"
    :disabled="disabled"
    :data-testid="dataTestid"
    @click="onToggle"
    :class="switchClass"
  >
    <span
      class="inline-block h-4 w-4 rounded-full bg-background shadow-sm transition-transform"
      :class="checked ? 'translate-x-[18px]' : 'translate-x-0.5'"
    />
  </button>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  checked: boolean
  label: string
  disabled?: boolean
  toggling?: boolean
  dataTestid?: string
}>()

const emit = defineEmits<{
  (e: 'toggle', next: boolean): void
}>()

function onToggle() {
  if (props.disabled) return
  emit('toggle', !props.checked)
}

const switchClass = computed(() => {
  if (props.toggling) return 'bg-muted-foreground/50'
  return props.checked ? 'bg-primary' : 'bg-input'
})
</script>
