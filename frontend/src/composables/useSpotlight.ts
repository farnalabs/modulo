import { ref, computed } from 'vue'

type SpotlightState = {
  targetTestId: string | null
  message: string | null
}

const _state = ref<SpotlightState>({ targetTestId: null, message: null })

export function useSpotlight() {
  const active = computed(() => _state.value.targetTestId !== null)
  const target = computed(() => _state.value.targetTestId)
  const message = computed(() => _state.value.message)
  const targetElement = computed(() => {
    if (!_state.value.targetTestId) return null
    return document.querySelector(`[data-testid="${CSS.escape(_state.value.targetTestId)}"]`) as HTMLElement | null
  })

  function highlight(testId: string, msg?: string) {
    _state.value = { targetTestId: testId, message: msg ?? null }
  }

  function dismiss() {
    _state.value = { targetTestId: null, message: null }
  }

  return { active, target, message, targetElement, highlight, dismiss }
}

export const spotlight = useSpotlight()
