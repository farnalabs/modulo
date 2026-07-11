<template>
  <Dialog v-model:open="modelValue" @update:open="emit('update:open', $event)">
    <DialogContent :class="dialogClass">
      <DialogHeader>
        <DialogTitle>{{ title }}</DialogTitle>
        <DialogDescription v-if="description">{{ description }}</DialogDescription>
      </DialogHeader>
      <slot />
      <DialogFooter class="gap-2 sm:justify-end">
        <Button variant="outline" @click="emit('update:open', false)">Cancel</Button>
        <Button :disabled="confirmDisabled" :loading="loading" @click="emit('confirm')">
          {{ confirmText || 'Confirm' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>

<script setup lang="ts">
import type { HTMLAttributes } from 'vue'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog'
import { Button } from '../ui/button'

defineProps<{
  title: string
  description?: string
  confirmText?: string
  confirmDisabled?: boolean
  loading?: boolean
  class?: HTMLAttributes['class']
}>()

const modelValue = defineModel<boolean>('open')
const emit = defineEmits<{
  confirm: []
  'update:open': [value: boolean]
}>()

const dialogClass = 'sm:max-w-md'
</script>
