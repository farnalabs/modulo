import { ref } from 'vue'

interface ConfirmDialogOptions {
  onOpen?: () => void
}

export function useConfirmDialog(options?: ConfirmDialogOptions) {
  const isOpen = ref(false)
  const isConfirming = ref(false)

  function open() {
    options?.onOpen?.()
    isOpen.value = true
  }

  function close() {
    isOpen.value = false
  }

  async function confirm(fn: () => Promise<void>) {
    isConfirming.value = true
    try {
      await fn()
      close()
    } catch (e) {
      throw e
    } finally {
      isConfirming.value = false
    }
  }

  return { isOpen, isConfirming, open, close, confirm }
}
