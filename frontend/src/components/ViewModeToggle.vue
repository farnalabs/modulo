<template>
  <div class="flex rounded-md border bg-muted p-0.5 mb-3 shrink-0">
    <button
      v-for="opt in options"
      :key="opt.value"
      class="flex-1 rounded px-2 py-1 text-xs font-medium transition-colors"
      :class="modelValue === opt.value ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'"
      @click="$emit('update:modelValue', opt.value)"
    >
      {{ opt.label }}
    </button>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  modelValue: string;
  options: { label: string; value: string }[];
}>();

defineEmits<{
  "update:modelValue": [value: string];
}>();
</script>
