<template>
  <router-link :to="to" :class="['sidebar-link', { active: isActive }]" :aria-current="isActive ? 'page' : undefined" v-bind="$attrs">
    <span class="h-4 w-4 shrink-0"><SvgIcon :name="icon" /></span>
    <span class="truncate">{{ label }}</span>
  </router-link>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import SvgIcon from './SvgIcon.vue'

const props = defineProps<{
  to: string
  icon: string
  label: string
}>()

const route = useRoute()

const isActive = computed(() => {
  return route.path === props.to || (props.to !== '/' && route.path.startsWith(props.to + '/'))
})
</script>
