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
  /* WCAG 2.5.8 target size: at least 24x24 CSS px touch target */
  min-height: 1.5rem;
  min-width: 1.5rem;
  padding: 0.375rem 0;
}

.back-link:hover {
  color: hsl(var(--primary));
}

.back-arrow {
  font-size: 1rem;
  line-height: 1;
}
</style>
