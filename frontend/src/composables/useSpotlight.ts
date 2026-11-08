import { ref, computed } from 'vue'

type SpotlightState = {
  targetTestId: string | null
  message: string | null
}

export type SpotlightStep = {
  targetTestId: string
  message: string
}

const _state = ref<SpotlightState>({ targetTestId: null, message: null })
const _steps = ref<SpotlightStep[]>([])
const _stepIndex = ref(-1)

function highlight(testId: string, msg?: string) {
  _steps.value = []
  _stepIndex.value = -1
  _state.value = { targetTestId: testId, message: msg ?? null }
}

function start(steps: SpotlightStep[]) {
  if (steps.length === 0) return
  _steps.value = steps
  _stepIndex.value = 0
  const step = steps[0]
  _state.value = { targetTestId: step.targetTestId, message: step.message }
}

function next() {
  // Nothing to advance through for a single highlight (or after dismiss):
  // `next` must never dismiss a highlight that has no guide behind it.
  if (_steps.value.length === 0) return
  const nextIndex = _stepIndex.value + 1
  if (nextIndex >= _steps.value.length) {
    dismiss()
    return
  }
  _stepIndex.value = nextIndex
  const step = _steps.value[nextIndex]
  _state.value = { targetTestId: step.targetTestId, message: step.message }
}

function dismiss() {
  _steps.value = []
  _stepIndex.value = -1
  _state.value = { targetTestId: null, message: null }
}

export function useSpotlight() {
  const active = computed(() => _state.value.targetTestId !== null)
  const target = computed(() => _state.value.targetTestId)
  const message = computed(() => _state.value.message)
  const hasNext = computed(() => _stepIndex.value >= 0 && _stepIndex.value < _steps.value.length - 1)
  const targetElement = computed(() => {
    if (!_state.value.targetTestId) return null
    return document.querySelector(`[data-testid="${CSS.escape(_state.value.targetTestId)}"]`) as HTMLElement | null
  })

  return { active, target, message, hasNext, targetElement, highlight, start, next, dismiss }
}

export const spotlight = useSpotlight()
