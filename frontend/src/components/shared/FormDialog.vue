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
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'

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
