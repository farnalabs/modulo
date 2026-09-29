import { onUnmounted, ref, type Ref } from 'vue'

export interface SecretRevealOptions {
  /**
   * Seconds the revealed value stays readable before the input masks itself.
   * Defaults to 10 - the value both the MCP API key and the OAuth client
   * secret dialogs shipped with.
   */
  revealSeconds?: number
}

export interface SecretReveal {
  /** The one-time secret. Empty whenever it is not currently being shown. */
  value: Ref<string>
  /** True while the input must render as a password field. */
  masked: Ref<boolean>
  /** Seconds left before `masked` flips to true. */
  countdown: Ref<number>
  /** Hold `secret` and start the countdown that masks it. */
  reveal: (secret: string) => void
  /**
   * Stop the countdown and wipe the secret from memory - a one-time
   * credential must not outlive the dialog that showed it (the
   * `AdminUsersView` `dismissCredentialState` convention). The component's
   * timer is also cleared automatically on unmount.
   */
  onClose: () => void
}

/**
 * The one-time-secret reveal pattern: hold a credential, show it in the clear
 * for a bounded countdown, mask it when the countdown elapses, and wipe it on
 * close. Shared by the MCP API key and OAuth client registration dialogs,
 * which implemented this verbatim twice before.
 *
 * The countdown is a plain `setInterval`, so it stops itself the moment the
 * value masks; `onClose` and unmount both guarantee no timer outlives the
 * dialog.
 */
export function useSecretReveal(options: SecretRevealOptions = {}): SecretReveal {
  const revealSeconds = options.revealSeconds ?? 10
  const value = ref('')
  const masked = ref(false)
  const countdown = ref(revealSeconds)
  let timer: ReturnType<typeof setInterval> | null = null

  function stop(): void {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  function reveal(secret: string): void {
    stop()
    value.value = secret
    masked.value = false
    countdown.value = revealSeconds
    timer = setInterval(() => {
      countdown.value--
      if (countdown.value <= 0) {
        masked.value = true
        stop()
      }
    }, 1000)
  }

  function onClose(): void {
    stop()
    value.value = ''
    masked.value = true
    countdown.value = revealSeconds
  }

  onUnmounted(stop)

  return { value, masked, countdown, reveal, onClose }
}
