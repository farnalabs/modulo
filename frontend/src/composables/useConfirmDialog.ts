import { ref, readonly } from 'vue'

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
    if (isConfirming.value) return
    isConfirming.value = true
    try {
      await fn()
      close()
    } finally {
      isConfirming.value = false
    }
  }

  return { isOpen: readonly(isOpen), isConfirming: readonly(isConfirming), open, close, confirm }
}
