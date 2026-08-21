<template>
  <span
    class="inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full transition-colors"
    role="switch"
    :aria-checked="checked"
    :aria-label="label"
    :tabindex="disabled ? -1 : 0"
    :disabled="disabled"
    :data-testid="dataTestid"
    @click="onToggle"
    @keydown.enter="onToggle"
    @keydown.space.prevent="onToggle"
    :class="toggling ? 'bg-muted-foreground/50' : (checked ? 'bg-primary' : 'bg-input')"
  >
    <span
      class="inline-block h-4 w-4 rounded-full bg-background shadow-sm transition-transform"
      :class="checked ? 'translate-x-[18px]' : 'translate-x-0.5'"
    />
  </span>
</template>

<script setup lang="ts">
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
  emit('toggle', !props.checked)
}
</script>
