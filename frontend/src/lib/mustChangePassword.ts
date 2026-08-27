import { ref } from 'vue'

// FAR-460: global "must change password" gate state.
//
// Set true when the login response or /me reports must_change_password=true;
// App.vue replaces the whole app surface with the forced change-password view
// until the user succeeds, so navigation is blocked by construction rather
// than by per-route guards.
const mustChangePassword = ref(false)

export function setMustChangePassword(value: boolean): void {
  mustChangePassword.value = value
}

export function useMustChangePassword(): typeof mustChangePassword {
  return mustChangePassword
}
