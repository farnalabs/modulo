/**
 * Typed-judge authoring helpers (ADR 066 D4/D19).
 *
 * The single wire contract is
 * ``backend/src/modulo/core/eval_engine/typed_judge/config.py``. Everything
 * this module emits must satisfy it exactly: snake_case keys, ``extra="forbid"``
 * at every level, a discriminated union on ``type`` for questions, and no
 * ``pass_threshold`` (that field is the OKR suite pass-rate alert, not the
 * per-eval cut — the cut is ``pass_rule.threshold``).
 *
 * The draft shape below is the editor's local model. It deliberately keeps
 * numeric inputs as strings so a cleared/partial input is a validation error
 * rather than a silent ``NaN``; ``buildTypedJudgeConfig`` parses them only
 * after ``validateTypedJudgeDraft`` has returned no errors.
 *
 * Nothing here composes a judge display label. ADR 066 §0 confines the
 * decision-model class phrase to the locale value and the backend naming
 * module; this module has no user-facing strings at all.
 */

/** The dialect marker on a typed judge config (ADR 066 D4). */
export const TYPED_ANSWER_CONTRACT = 'typed_questions'

/** Bounds mirrored from the backend schema. */
export const MIN_ANSWERS = 2
export const MAX_CHOICE_OPTIONS = 255
export const MAX_SCORE_LEVELS = 10

export type JudgeMechanism = 'typed_questions' | 'deterministic'
export type QuestionType = 'noul' | 'choice' | 'score'
export type BackingKind = 'model_backend' | 'decision_model'
export type VerdictKind = 'assessment' | 'derivation'

/**
 * A numeric draft value. Vue's `v-model` casts `<input type="number">` to a
 * number, while a cleared field stays `''`; both shapes reach the helpers, so
 * they accept either rather than assuming a string and crashing on `.trim()`.
 */
export type NumericDraftValue = string | number

export interface ChoiceOptionDraft {
  key: string
  description: string
  passing: boolean
  /** Optional per-option score (0..1). Empty means "not supplied". */
  score: NumericDraftValue
}

export interface ScoreLevelDraft {
  score: NumericDraftValue
  description: string
}

export interface QuestionDraft {
  id: string
  type: QuestionType
  instructions: string
  /** One criterion per line. */
  criteriaText: string
  weight: NumericDraftValue
  required: boolean
  minPassConfidence: NumericDraftValue
  options: ChoiceOptionDraft[]
  levels: ScoreLevelDraft[]
  passFrom: NumericDraftValue
}

export interface TypedJudgeDraft {
  field: string
  judgeWholeOutput: boolean
  /** Comma- or newline-separated dotted paths. */
  contextFields: string
  backingKind: BackingKind
  modelBackendId: string
  decisionVendor: string
  /** Optional decision-model class (``backing.model_class``), preserved as authored. */
  decisionModelClass: string
  /**
   * The stored cascade block, carried through the editor untouched (its
   * execution UI is a later slice, ADR 066 D7/S4). ``null`` means the config
   * had no cascade; an object is re-emitted verbatim so an author who opens and
   * saves an API/config-as-code definition never loses it.
   */
  cascade: Record<string, unknown> | null
  questions: QuestionDraft[]
  threshold: NumericDraftValue
  verdictKind: VerdictKind
  perQuestionSeconds: NumericDraftValue
  perDefinitionSeconds: NumericDraftValue
}

export interface JudgingValidationError {
  /** Dotted location inside the config, e.g. ``questions[0].weight``. */
  path: string
  /** Locale key (under ``views.EvalEditorView.judging.errors``). */
  key: string
  params?: Record<string, string | number>
}

export function parseNumber(raw: NumericDraftValue): number | null {
  const trimmed = String(raw).trim()
  if (trimmed === '') return null
  const value = Number(trimmed)
  return Number.isFinite(value) ? value : null
}

export function splitLines(raw: string): string[] {
  return raw
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '')
}

export function splitContextFields(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter((item) => item !== '')
}

function isDottedPath(value: string): boolean {
  return value !== '' && value.split('.').every((segment) => segment !== '')
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function emptyChoiceOption(): ChoiceOptionDraft {
  return { key: '', description: '', passing: false, score: '' }
}

export function createQuestion(type: QuestionType, id: string): QuestionDraft {
  const question: QuestionDraft = {
    id,
    type,
    instructions: '',
    criteriaText: '',
    weight: '1',
    required: false,
    minPassConfidence: '0.5',
    options: [],
    levels: [],
    passFrom: '0.5',
  }
  seedAnswerSpace(question)
  return question
}

/** Give a newly-typed question the minimum legal answer space. */
export function seedAnswerSpace(question: QuestionDraft): void {
  if (question.type === 'choice' && question.options.length < MIN_ANSWERS) {
    question.options = Array.from({ length: MIN_ANSWERS }, emptyChoiceOption)
  }
  if (question.type === 'score' && question.levels.length < MIN_ANSWERS) {
    question.levels = [
      { score: '0', description: '' },
      { score: '1', description: '' },
    ]
    question.passFrom = '0.5'
  }
}

export function createDefaultDraft(): TypedJudgeDraft {
  return {
    field: 'output',
    judgeWholeOutput: false,
    contextFields: '',
    backingKind: 'model_backend',
    modelBackendId: '',
    decisionVendor: 'Jev',
    decisionModelClass: '',
    cascade: null,
    questions: [createQuestion('noul', 'verdict')],
    threshold: '0.5',
    verdictKind: 'assessment',
    perQuestionSeconds: '10',
    perDefinitionSeconds: '45',
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function readString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function readNumberString(value: unknown, fallback: NumericDraftValue): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : String(fallback)
}

/**
 * Read a stored typed config back into the editor's draft model.
 *
 * Returns ``null`` when the config is not the typed dialect (a legacy prompt
 * blob, ADR 066 D8) so the caller can show the read-only legacy state instead
 * of pretending it is editable typed data.
 */
export function draftFromConfig(config: unknown): TypedJudgeDraft | null {
  if (!isRecord(config) || config.answer_contract !== TYPED_ANSWER_CONTRACT) return null
  const draft = createDefaultDraft()
  draft.field = readString(config.field, draft.field)
  draft.judgeWholeOutput = config.judge_whole_output === true
  draft.contextFields = Array.isArray(config.context_fields)
    ? config.context_fields.filter((item): item is string => typeof item === 'string').join(', ')
    : ''

  const backing = isRecord(config.backing) ? config.backing : {}
  draft.backingKind = backing.kind === 'decision_model' ? 'decision_model' : 'model_backend'
  draft.modelBackendId = readString(backing.model_backend_id)
  draft.decisionVendor = readString(backing.vendor, draft.decisionVendor)
  draft.decisionModelClass = readString(backing.model_class)
  // Preserve the cascade block (and any key the server schema gains later)
  // rather than silently dropping a valid definition on save.
  draft.cascade = isRecord(config.cascade) ? structuredClone(config.cascade) : null

  const questions = Array.isArray(config.questions) ? config.questions : []
  draft.questions = questions.filter(isRecord).map((raw, index) => {
    const type: QuestionType =
      raw.type === 'choice' || raw.type === 'score' ? raw.type : 'noul'
    const question = createQuestion(type, readString(raw.id, `q${index + 1}`))
    question.instructions = readString(raw.instructions)
    question.criteriaText = Array.isArray(raw.criteria)
      ? raw.criteria.filter((item): item is string => typeof item === 'string').join('\n')
      : ''
    question.weight = readNumberString(raw.weight, question.weight)
    question.required = raw.required === true
    question.minPassConfidence = readNumberString(raw.min_pass_confidence, question.minPassConfidence)

    if (type === 'choice') {
      const passing = new Set(
        Array.isArray(raw.passing_keys)
          ? raw.passing_keys.filter((item): item is string => typeof item === 'string')
          : [],
      )
      const scores = isRecord(raw.option_scores) ? raw.option_scores : {}
      const options = Array.isArray(raw.options) ? raw.options : []
      question.options = options.filter(isRecord).map((option) => {
        const key = readString(option.key)
        return {
          key,
          description: readString(option.description),
          passing: passing.has(key),
          score: readNumberString(scores[key], ''),
        }
      })
      seedAnswerSpace(question)
    }

    if (type === 'score') {
      const levels = Array.isArray(raw.levels) ? raw.levels : []
      question.levels = levels.filter(isRecord).map((level) => ({
        score: readNumberString(level.score, ''),
        description: readString(level.description),
      }))
      question.passFrom = readNumberString(raw.pass_from, question.passFrom)
      seedAnswerSpace(question)
    }

    return question
  })
  if (draft.questions.length === 0) {
    draft.questions = [createQuestion('noul', 'verdict')]
  }

  const passRule = isRecord(config.pass_rule) ? config.pass_rule : {}
  draft.threshold = readNumberString(passRule.threshold, draft.threshold)

  const timeouts = isRecord(config.timeouts) ? config.timeouts : {}
  draft.perQuestionSeconds = readNumberString(
    timeouts.per_question_seconds,
    draft.perQuestionSeconds,
  )
  draft.perDefinitionSeconds = readNumberString(
    timeouts.per_definition_seconds,
    draft.perDefinitionSeconds,
  )

  draft.verdictKind = config.verdict_kind === 'derivation' ? 'derivation' : 'assessment'
  return draft
}

function buildBacking(draft: TypedJudgeDraft): Record<string, unknown> {
  if (draft.backingKind === 'decision_model') {
    const backing: Record<string, unknown> = {
      kind: 'decision_model',
      vendor: draft.decisionVendor.trim(),
    }
    const modelClass = draft.decisionModelClass.trim()
    if (modelClass !== '') backing.model_class = modelClass
    return backing
  }
  const backendId = draft.modelBackendId.trim()
  return backendId
    ? { kind: 'model_backend', model_backend_id: backendId }
    : { kind: 'model_backend' }
}

function buildQuestion(question: QuestionDraft): Record<string, unknown> {
  const base: Record<string, unknown> = {
    id: question.id.trim(),
    type: question.type,
    instructions: question.instructions,
    criteria: splitLines(question.criteriaText),
    weight: parseNumber(question.weight) ?? Number.NaN,
    required: question.required,
    min_pass_confidence: parseNumber(question.minPassConfidence) ?? Number.NaN,
  }

  if (question.type === 'noul') return base

  if (question.type === 'choice') {
    const options = question.options.map((option) => ({
      key: option.key.trim(),
      description: option.description,
    }))
    const optionScores: Record<string, number> = {}
    for (const option of question.options) {
      const score = parseNumber(option.score)
      if (score !== null) optionScores[option.key.trim()] = score
    }
    const built: Record<string, unknown> = {
      ...base,
      options,
      passing_keys: question.options
        .filter((option) => option.passing)
        .map((option) => option.key.trim()),
    }
    if (Object.keys(optionScores).length > 0) built.option_scores = optionScores
    return built
  }

  return {
    ...base,
    levels: question.levels.map((level) => ({
      score: parseNumber(level.score) ?? Number.NaN,
      description: level.description,
    })),
    pass_from: parseNumber(question.passFrom) ?? Number.NaN,
  }
}

/**
 * Emit the exact wire payload the API's ``TypedJudgeConfig`` accepts.
 *
 * Callers must validate first (``validateTypedJudgeDraft``); an invalid draft
 * still emits its parsed values (``NaN`` becomes ``null`` through
 * ``JSON.stringify``) so a mistake can never be silently normalised away.
 */
export function buildTypedJudgeConfig(draft: TypedJudgeDraft): Record<string, unknown> {
  const config: Record<string, unknown> = {
    answer_contract: TYPED_ANSWER_CONTRACT,
    field: draft.judgeWholeOutput ? '' : draft.field.trim(),
    context_fields: splitContextFields(draft.contextFields),
    backing: buildBacking(draft),
    questions: draft.questions.map(buildQuestion),
    pass_rule: {
      kind: 'weighted_score_at_least',
      threshold: parseNumber(draft.threshold) ?? Number.NaN,
    },
    verdict_kind: draft.verdictKind,
    timeouts: {
      per_question_seconds: parseNumber(draft.perQuestionSeconds) ?? Number.NaN,
      per_definition_seconds: parseNumber(draft.perDefinitionSeconds) ?? Number.NaN,
    },
  }
  if (draft.judgeWholeOutput) config.judge_whole_output = true
  // Re-emit the stored cascade verbatim; the editor does not author it yet, so
  // dropping it would silently rewrite an API/config-as-code definition.
  if (draft.cascade !== null) config.cascade = draft.cascade
  return config
}

function validateBacking(
  draft: TypedJudgeDraft,
  errors: JudgingValidationError[],
): void {
  if (draft.backingKind === 'decision_model') {
    if (draft.decisionVendor.trim() === '') {
      errors.push({ path: 'backing.vendor', key: 'backingVendorRequired' })
    }
    return
  }
  const backendId = draft.modelBackendId.trim()
  if (backendId !== '' && !UUID_PATTERN.test(backendId)) {
    errors.push({ path: 'backing.model_backend_id', key: 'backingIdInvalid' })
  }
}

function validateChoiceQuestion(
  question: QuestionDraft,
  index: number,
  errors: JudgingValidationError[],
): void {
  if (question.options.length < MIN_ANSWERS || question.options.length > MAX_CHOICE_OPTIONS) {
    errors.push({
      path: `questions[${index}].options`,
      key: 'choiceOptionsRange',
      params: { min: MIN_ANSWERS, max: MAX_CHOICE_OPTIONS },
    })
  }

  const seen = new Set<string>()
  for (const [optionIndex, option] of question.options.entries()) {
    const key = option.key.trim()
    if (key === '') {
      errors.push({
        path: `questions[${index}].options[${optionIndex}].key`,
        key: 'choiceOptionKeyRequired',
        params: { option: optionIndex + 1 },
      })
      continue
    }
    if (seen.has(key)) {
      errors.push({
        path: `questions[${index}].options[${optionIndex}].key`,
        key: 'choiceOptionKeyDuplicate',
        params: { key },
      })
    }
    seen.add(key)
    if (String(option.score).trim() !== '') {
      const score = parseNumber(option.score)
      if (score === null || score < 0 || score > 1) {
        errors.push({
          path: `questions[${index}].options[${optionIndex}].score`,
          key: 'choiceOptionScoreInvalid',
          params: { key },
        })
      }
    }
  }

  if (!question.options.some((option) => option.passing)) {
    errors.push({
      path: `questions[${index}].passing_keys`,
      key: 'choicePassingRequired',
    })
  }
}

function validateScoreQuestion(
  question: QuestionDraft,
  index: number,
  errors: JudgingValidationError[],
): void {
  if (question.levels.length < MIN_ANSWERS || question.levels.length > MAX_SCORE_LEVELS) {
    errors.push({
      path: `questions[${index}].levels`,
      key: 'scoreLevelsRange',
      params: { min: MIN_ANSWERS, max: MAX_SCORE_LEVELS },
    })
  }

  const values: number[] = []
  for (const [levelIndex, level] of question.levels.entries()) {
    const value = parseNumber(level.score)
    if (value === null) {
      errors.push({
        path: `questions[${index}].levels[${levelIndex}].score`,
        key: 'scoreLevelRequired',
        params: { level: levelIndex + 1 },
      })
      continue
    }
    values.push(value)
  }

  if (new Set(values).size !== values.length) {
    errors.push({ path: `questions[${index}].levels`, key: 'scoreLevelDuplicate' })
  }
  const ordered = [...values].sort((a, b) => a - b)
  if (values.length > 1 && values.some((value, position) => value !== ordered[position])) {
    errors.push({ path: `questions[${index}].levels`, key: 'scoreLevelOrder' })
  }

  const passFrom = parseNumber(question.passFrom)
  if (passFrom === null) {
    errors.push({ path: `questions[${index}].pass_from`, key: 'scorePassFromInvalid' })
  } else if (values.length > 0) {
    const min = Math.min(...values)
    const max = Math.max(...values)
    if (passFrom < min || passFrom > max) {
      errors.push({ path: `questions[${index}].pass_from`, key: 'scorePassFromInvalid' })
    }
  }
}

function validateQuestion(
  question: QuestionDraft,
  index: number,
  errors: JudgingValidationError[],
): void {
  if (question.id.trim() === '') {
    errors.push({ path: `questions[${index}].id`, key: 'questionIdRequired', params: { index: index + 1 } })
  }

  const weight = parseNumber(question.weight)
  if (weight === null || weight <= 0) {
    errors.push({ path: `questions[${index}].weight`, key: 'weightInvalid', params: { index: index + 1 } })
  }

  const minConfidence = parseNumber(question.minPassConfidence)
  if (minConfidence === null || minConfidence < 0 || minConfidence > 1) {
    errors.push({
      path: `questions[${index}].min_pass_confidence`,
      key: 'minConfidenceInvalid',
      params: { index: index + 1 },
    })
  }

  if (question.type === 'choice') validateChoiceQuestion(question, index, errors)
  if (question.type === 'score') validateScoreQuestion(question, index, errors)
}

function validateFieldSelection(draft: TypedJudgeDraft, errors: JudgingValidationError[]): void {
  if (draft.verdictKind === 'derivation') {
    errors.push({ path: 'verdict_kind', key: 'verdictDerivation' })
  }

  if (!draft.judgeWholeOutput) {
    const field = draft.field.trim()
    if (field === '') {
      errors.push({ path: 'field', key: 'fieldRequired' })
    } else if (!isDottedPath(field)) {
      errors.push({ path: 'field', key: 'fieldNotDotted' })
    }
  }

  const contextFields = splitContextFields(draft.contextFields)
  if (new Set(contextFields).size !== contextFields.length) {
    errors.push({ path: 'context_fields', key: 'contextFieldDuplicate' })
  }
  const field = draft.field.trim()
  if (!draft.judgeWholeOutput && field !== '' && contextFields.includes(field)) {
    errors.push({ path: 'context_fields', key: 'contextFieldEqualsField' })
  }
}

function validateQuestionList(draft: TypedJudgeDraft, errors: JudgingValidationError[]): void {
  if (draft.questions.length === 0) {
    errors.push({ path: 'questions', key: 'questionsRequired' })
  }

  const seenIds = new Set<string>()
  for (const [index, question] of draft.questions.entries()) {
    const id = question.id.trim()
    if (id !== '' && seenIds.has(id)) {
      errors.push({ path: `questions[${index}].id`, key: 'questionIdDuplicate', params: { id } })
    }
    seenIds.add(id)
    validateQuestion(question, index, errors)
  }
}

function validateThresholdsAndTimeouts(
  draft: TypedJudgeDraft,
  errors: JudgingValidationError[],
): void {
  const threshold = parseNumber(draft.threshold)
  if (threshold === null || threshold < 0 || threshold > 1) {
    errors.push({ path: 'pass_rule.threshold', key: 'thresholdInvalid' })
  }

  const perQuestion = parseNumber(draft.perQuestionSeconds)
  const perDefinition = parseNumber(draft.perDefinitionSeconds)
  if (perQuestion === null || perQuestion <= 0) {
    errors.push({ path: 'timeouts.per_question_seconds', key: 'timeoutsInvalid' })
  }
  if (perDefinition === null || perDefinition <= 0) {
    errors.push({ path: 'timeouts.per_definition_seconds', key: 'timeoutsInvalid' })
  } else if (perQuestion !== null && perDefinition < perQuestion) {
    errors.push({
      path: 'timeouts.per_definition_seconds',
      key: 'definitionTimeoutTooShort',
    })
  }
}

/**
 * Mirror the backend's write-time rules so an author sees the problem before
 * submitting. The server stays authoritative: its typed 422 codes are surfaced
 * inline too, because this cannot check backend existence/ownership.
 */
export function validateTypedJudgeDraft(draft: TypedJudgeDraft): JudgingValidationError[] {
  const errors: JudgingValidationError[] = []

  validateFieldSelection(draft, errors)
  validateQuestionList(draft, errors)
  validateThresholdsAndTimeouts(draft, errors)
  validateBacking(draft, errors)
  return errors
}
