<template>
  <router-link :to="to" class="back-link">
    <span class="back-arrow">&larr;</span>
    {{ label }}
  </router-link>
</template>

<script setup lang="ts">
defineProps<{
  to: string
  label: string
}>()
</script>

<style scoped>
.back-link {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  font-size: 0.875rem;
  color: hsl(var(--muted-foreground));
  text-decoration: none;
}

.back-link:hover {
  color: hsl(var(--primary));
}

.back-arrow {
  font-size: 1rem;
  line-height: 1;
}
</style>
