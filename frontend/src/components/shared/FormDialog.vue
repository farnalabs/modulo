<template>
  <Dialog v-model:visible="modelValue" :modal="true" :dismissable-mask="true" :style="{ width: '28rem' }">
    <template #header>
      <div>
        <div class="text-lg font-semibold">{{ title }}</div>
        <div v-if="description" class="mt-0.5 text-sm text-muted-foreground">{{ description }}</div>
      </div>
    </template>
    <slot />
    <template #footer>
      <div class="flex gap-2 justify-end">
        <Button severity="secondary" outlined @click="modelValue = false">{{ $t('common.cancel') }}</Button>
        <Button :disabled="confirmDisabled || loading" :loading="loading" @click="emit('confirm')">
          {{ confirmText || 'Confirm' }}
        </Button>
      </div>
    </template>
  </Dialog>
</template>

<script setup lang="ts">
import { provide } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import { SELECT_APPEND_TARGET } from './selectAppendTarget'

// FAR-1908: the dialog body is a scroll container, so an AppSelect overlay
// rendered inside it (`appendTo: 'self'`) is clipped by it and its lower
// options end up hidden behind the footer. Portal the overlays to <body>.
provide(SELECT_APPEND_TARGET, 'body')

const props = defineProps<{
  title: string
  description?: string
  confirmText?: string
  confirmDisabled?: boolean
  loading?: boolean
}>()

const modelValue = defineModel<boolean>('open')
const emit = defineEmits<{
  confirm: []
}>()
</script>
