<template>
  <div :role="role" :class="classes" :data-testid="dataTestid">
    <slot />
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  variant: "error" | "success";
  dataTestid?: string;
}>();

const role = computed(() => (props.variant === "error" ? "alert" : "status"));
const classes = computed(() =>
  props.variant === "error"
    ? "rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive"
    : "rounded-lg border border-emerald-500/50 bg-emerald-500/10 p-4 text-emerald-600",
);
</script>
