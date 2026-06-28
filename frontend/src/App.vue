<template>
  <LoginView v-if="!isAuthenticated" />
  <AppLayout v-else />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { getAccessToken } from './lib/api/client'
import LoginView from './views/LoginView.vue'
import AppLayout from './components/AppLayout.vue'

const isAuthenticated = computed(() => !!getAccessToken())
</script>
