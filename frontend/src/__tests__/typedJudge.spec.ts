// Unit coverage for the typed-judge authoring helpers (ADR 066 D4/D19).
// The wire contract under test is
// backend/src/modulo/core/eval_engine/typed_judge/config.py.
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'fs'
import { join, relative } from 'path'

import {
  MAX_CHOICE_OPTIONS,
  MAX_SCORE_LEVELS,
  TYPED_ANSWER_CONTRACT,
  buildTypedJudgeConfig,
  createDefaultDraft,
  createQuestion,
  draftFromConfig,
  validateTypedJudgeDraft,
  type TypedJudgeDraft,
} from '../components/eval/typedJudge'

const SRC_ROOT = join(__dirname, '..')

const MODEL_BACKEND_ID = '11111111-1111-4111-8111-111111111111'

/**
 * The exact payload the builder must emit. It mirrors the server's
 * ``TypedJudgeConfig``: snake_case, ``extra="forbid"`` at every level, and the
 * cut lives in ``pass_rule.threshold`` (never ``pass_threshold``).
 */
const WIRE_FIXTURE: Record<string, unknown> = {
  answer_contract: 'typed_questions',
  field: 'output.text',
  context_fields: ['input', 'output.metadata'],
  backing: { kind: 'model_backend', model_backend_id: MODEL_BACKEND_ID },
  questions: [
    {
      id: 'accurate',
      type: 'noul',
      instructions: 'Is the answer accurate?',
      criteria: ['No invented facts'],
      weight: 2,
      required: true,
      min_pass_confidence: 0.7,
    },
    {
      id: 'tone',
      type: 'choice',
      instructions: 'Pick the tone',
      criteria: [],
      weight: 1,
      required: false,
      min_pass_confidence: 0.5,
      options: [
        { key: 'neutral', description: 'Plain' },
        { key: 'friendly', description: 'Warm' },
      ],
      passing_keys: ['neutral'],
      option_scores: { neutral: 1, friendly: 0.5 },
    },
    {
      id: 'quality',
      type: 'score',
      instructions: 'Rate quality',
      criteria: [],
      weight: 1.5,
      required: false,
      min_pass_confidence: 0.5,
      levels: [
        { score: 0, description: 'Bad' },
        { score: 1, description: 'Good' },
      ],
      pass_from: 1,
    },
  ],
  pass_rule: { kind: 'weighted_score_at_least', threshold: 0.6 },
  verdict_kind: 'assessment',
  timeouts: { per_question_seconds: 10, per_definition_seconds: 45 },
}

const ROOT_KEYS = new Set([
  'answer_contract',
  'field',
  'context_fields',
  'judge_whole_output',
  'backing',
  'questions',
  'pass_rule',
  'verdict_kind',
  'cascade',
  'timeouts',
])
const QUESTION_KEYS: Record<string, Set<string>> = {
  noul: new Set(['id', 'type', 'instructions', 'criteria', 'weight', 'required', 'min_pass_confidence']),
  choice: new Set([
    'id',
    'type',
    'instructions',
    'criteria',
    'weight',
    'required',
    'min_pass_confidence',
    'options',
    'passing_keys',
    'option_scores',
  ]),
  score: new Set([
    'id',
    'type',
    'instructions',
    'criteria',
    'weight',
    'required',
    'min_pass_confidence',
    'levels',
    'pass_from',
  ]),
}

function errorKeys(draft: TypedJudgeDraft): string[] {
  return validateTypedJudgeDraft(draft).map((error) => error.key)
}

function draftFromFixture(): TypedJudgeDraft {
  const draft = draftFromConfig(WIRE_FIXTURE)
  expect(draft).not.toBeNull()
  return draft as TypedJudgeDraft
}

describe('typedJudge — default draft and builder', () => {
  it('builds a valid default typed config', () => {
    const config = buildTypedJudgeConfig(createDefaultDraft())
    expect(validateTypedJudgeDraft(createDefaultDraft())).toEqual([])
    expect(config.answer_contract).toBe(TYPED_ANSWER_CONTRACT)
    expect(config.field).toBe('output')
    expect(config.backing).toEqual({ kind: 'model_backend' })
    expect(config.pass_rule).toEqual({ kind: 'weighted_score_at_least', threshold: 0.5 })
    expect(config.verdict_kind).toBe('assessment')
    expect(config).not.toHaveProperty('pass_threshold')
  })

  it('omits judge_whole_output unless explicitly enabled, and empties the field then', () => {
    const draft = createDefaultDraft()
    draft.judgeWholeOutput = true
    const config = buildTypedJudgeConfig(draft)
    expect(config.judge_whole_output).toBe(true)
    expect(config.field).toBe('')
  })

  it('emits only the declared decision-model backing keys', () => {
    const draft = createDefaultDraft()
    draft.backingKind = 'decision_model'
    draft.decisionVendor = 'Jev'
    expect(buildTypedJudgeConfig(draft).backing).toEqual({ kind: 'decision_model', vendor: 'Jev' })
  })

  it('emits option_scores only when at least one score is supplied', () => {
    const draft = createDefaultDraft()
    const question = createQuestion('choice', 'tone')
    question.options[0].key = 'a'
    question.options[0].passing = true
    question.options[1].key = 'b'
    draft.questions = [question]
    expect(buildTypedJudgeConfig(draft).questions).toEqual([
      expect.objectContaining({ options: [{ key: 'a', description: '' }, { key: 'b', description: '' }] }),
    ])
    expect(
      ((buildTypedJudgeConfig(draft).questions as Record<string, unknown>[])[0] as Record<string, unknown>)
        .option_scores,
    ).toBeUndefined()

    question.options[0].score = '1'
    expect(
      ((buildTypedJudgeConfig(draft).questions as Record<string, unknown>[])[0] as Record<string, unknown>)
        .option_scores,
    ).toEqual({ a: 1 })
  })
})

describe('typedJudge — contract round-trip', () => {
  it('round-trips a stored typed config without drift', () => {
    expect(buildTypedJudgeConfig(draftFromFixture())).toEqual(WIRE_FIXTURE)
  })

  it('emits no camelCase key and no key outside the server schema', () => {
    const config = buildTypedJudgeConfig(draftFromFixture())
    const seenKeys: string[] = []

    function checkKeys(record: Record<string, unknown>, allowed: Set<string>, where: string): void {
      for (const key of Object.keys(record)) {
        expect(key, `${where}.${key} must be snake_case`).not.toMatch(/[A-Z]/)
        expect(allowed.has(key), `${where}.${key} is not in the wire schema`).toBe(true)
        seenKeys.push(`${where}.${key}`)
      }
    }

    checkKeys(config, ROOT_KEYS, 'config')
    checkKeys(config.backing as Record<string, unknown>, new Set(['kind', 'model_backend_id']), 'backing')
    checkKeys(
      config.pass_rule as Record<string, unknown>,
      new Set(['kind', 'threshold']),
      'pass_rule',
    )
    checkKeys(
      config.timeouts as Record<string, unknown>,
      new Set(['per_question_seconds', 'per_definition_seconds']),
      'timeouts',
    )

    for (const [index, question] of (config.questions as Record<string, unknown>[]).entries()) {
      const type = String(question.type)
      checkKeys(question, QUESTION_KEYS[type], `questions[${index}]`)
      for (const option of (question.options as Record<string, unknown>[] | undefined) ?? []) {
        checkKeys(option, new Set(['key', 'description']), `questions[${index}].option`)
      }
      for (const level of (question.levels as Record<string, unknown>[] | undefined) ?? []) {
        checkKeys(level, new Set(['score', 'description']), `questions[${index}].level`)
      }
    }

    // Prove the walk actually covered the payload (a negative search alone
    // would pass on an empty object).
    expect(seenKeys).toContain('config.answer_contract')
    expect(seenKeys).toContain('questions[1].passing_keys')
    expect(seenKeys).toContain('questions[2].pass_from')
  })

  it('bounds match the server schema', () => {
    expect(MAX_CHOICE_OPTIONS).toBe(255)
    expect(MAX_SCORE_LEVELS).toBe(10)
  })
})

describe('typedJudge — draftFromConfig', () => {
  it('returns null for a legacy prompt config', () => {
    expect(draftFromConfig({ field: 'output', instructions: 'Judge it' })).toBeNull()
    expect(draftFromConfig(null)).toBeNull()
  })

  it('recovers question types, passing keys, option scores and levels', () => {
    const draft = draftFromFixture()
    expect(draft.questions.map((question) => question.type)).toEqual(['noul', 'choice', 'score'])
    expect(draft.questions[1].options.map((option) => option.passing)).toEqual([true, false])
    expect(draft.questions[1].options.map((option) => option.score)).toEqual(['1', '0.5'])
    expect(draft.questions[2].levels.map((level) => level.score)).toEqual(['0', '1'])
    expect(draft.verdictKind).toBe('assessment')
  })
})

describe('typedJudge — client validation mirrors the server bounds', () => {
  it('accepts the round-tripped fixture', () => {
    expect(validateTypedJudgeDraft(draftFromFixture())).toEqual([])
  })

  it('rejects derivation with its own code', () => {
    const draft = createDefaultDraft()
    draft.verdictKind = 'derivation'
    expect(errorKeys(draft)).toContain('verdictDerivation')
  })

  it('requires a dotted field unless the whole output is judged', () => {
    const missing = createDefaultDraft()
    missing.field = ''
    expect(errorKeys(missing)).toContain('fieldRequired')

    const malformed = createDefaultDraft()
    malformed.field = 'output..text'
    expect(errorKeys(malformed)).toContain('fieldNotDotted')

    const whole = createDefaultDraft()
    whole.judgeWholeOutput = true
    whole.field = ''
    expect(errorKeys(whole)).not.toContain('fieldRequired')
  })

  it('rejects duplicate context fields and a repeated primary field', () => {
    const duplicate = createDefaultDraft()
    duplicate.contextFields = 'input, input'
    expect(errorKeys(duplicate)).toContain('contextFieldDuplicate')

    const repeated = createDefaultDraft()
    repeated.field = 'output'
    repeated.contextFields = 'output'
    expect(errorKeys(repeated)).toContain('contextFieldEqualsField')
  })

  it('requires at least one question and unique ids', () => {
    const empty = createDefaultDraft()
    empty.questions = []
    expect(errorKeys(empty)).toContain('questionsRequired')

    const duplicate = createDefaultDraft()
    duplicate.questions = [createQuestion('noul', 'same'), createQuestion('noul', 'same')]
    expect(errorKeys(duplicate)).toContain('questionIdDuplicate')
  })

  it('rejects a non-positive weight and an out-of-range confidence', () => {
    const zeroWeight = createDefaultDraft()
    zeroWeight.questions[0].weight = '0'
    expect(errorKeys(zeroWeight)).toContain('weightInvalid')

    const badConfidence = createDefaultDraft()
    badConfidence.questions[0].minPassConfidence = '1.5'
    expect(errorKeys(badConfidence)).toContain('minConfidenceInvalid')
  })

  it('rejects choice answer-space violations', () => {
    const tooFew = createDefaultDraft()
    const single = createQuestion('choice', 'tone')
    single.options = single.options.slice(0, 1)
    single.options[0].key = 'only'
    single.options[0].passing = true
    tooFew.questions = [single]
    expect(errorKeys(tooFew)).toContain('choiceOptionsRange')

    const duplicateKeys = createDefaultDraft()
    const dup = createQuestion('choice', 'tone')
    dup.options[0].key = 'same'
    dup.options[1].key = 'same'
    dup.options[0].passing = true
    duplicateKeys.questions = [dup]
    expect(errorKeys(duplicateKeys)).toContain('choiceOptionKeyDuplicate')

    const noPassing = createDefaultDraft()
    const noPass = createQuestion('choice', 'tone')
    noPass.options[0].key = 'a'
    noPass.options[1].key = 'b'
    noPassing.questions = [noPass]
    expect(errorKeys(noPassing)).toContain('choicePassingRequired')

    const badScore = createDefaultDraft()
    const bad = createQuestion('choice', 'tone')
    bad.options[0].key = 'a'
    bad.options[0].passing = true
    bad.options[0].score = '2'
    bad.options[1].key = 'b'
    badScore.questions = [bad]
    expect(errorKeys(badScore)).toContain('choiceOptionScoreInvalid')
  })

  it('rejects score scale violations', () => {
    const tooFew = createDefaultDraft()
    const single = createQuestion('score', 'quality')
    single.levels = single.levels.slice(0, 1)
    tooFew.questions = [single]
    expect(errorKeys(tooFew)).toContain('scoreLevelsRange')

    const duplicate = createDefaultDraft()
    const dup = createQuestion('score', 'quality')
    dup.levels[1].score = '0'
    duplicate.questions = [dup]
    expect(errorKeys(duplicate)).toContain('scoreLevelDuplicate')

    const unordered = createDefaultDraft()
    const order = createQuestion('score', 'quality')
    order.levels[0].score = '1'
    order.levels[1].score = '0'
    order.passFrom = '0.5'
    unordered.questions = [order]
    expect(errorKeys(unordered)).toContain('scoreLevelOrder')

    const passFrom = createDefaultDraft()
    const range = createQuestion('score', 'quality')
    range.passFrom = '5'
    passFrom.questions = [range]
    expect(errorKeys(passFrom)).toContain('scorePassFromInvalid')
  })

  it('rejects an out-of-range pass-rule threshold and bad timeouts', () => {
    const threshold = createDefaultDraft()
    threshold.threshold = '1.5'
    expect(errorKeys(threshold)).toContain('thresholdInvalid')

    const timeouts = createDefaultDraft()
    timeouts.perQuestionSeconds = '10'
    timeouts.perDefinitionSeconds = '5'
    expect(errorKeys(timeouts)).toContain('definitionTimeoutTooShort')
  })

  it('rejects a decision model without a vendor and a malformed backend id', () => {
    const noVendor = createDefaultDraft()
    noVendor.backingKind = 'decision_model'
    noVendor.decisionVendor = ' '
    expect(errorKeys(noVendor)).toContain('backingVendorRequired')

    const badId = createDefaultDraft()
    badId.modelBackendId = 'not-a-uuid'
    expect(errorKeys(badId)).toContain('backingIdInvalid')
  })
})

// ---------------------------------------------------------------------------
// ADR 066 §0 naming rule — structural enforcement on the frontend side.
// ---------------------------------------------------------------------------

function collectFiles(root: string, predicate: (path: string) => boolean): string[] {
  const found: string[] = []
  for (const entry of readdirSync(root)) {
    const full = join(root, entry)
    if (statSync(full).isDirectory()) {
      found.push(...collectFiles(full, predicate))
      continue
    }
    if (predicate(full)) found.push(full)
  }
  return found
}

describe('ADR 066 §0 naming rule', () => {
  it('holds exactly one "System One" value in the default locale, and it is the model-class label', () => {
    const source = readFileSync(join(SRC_ROOT, 'locales', 'en-US.js'), 'utf-8')
    const matches = source.match(/System One/g) ?? []
    expect(matches).toHaveLength(1)
    expect(source).toContain('"systemOneModel": "System One model"')
  })

  it('no component or helper composes the phrase itself', () => {
    const files = collectFiles(
      SRC_ROOT,
      (path) =>
        (path.endsWith('.vue') || path.endsWith('.ts') || path.endsWith('.js')) &&
        !path.includes(`${join('src', 'locales')}`) &&
        !path.includes(`${join('src', '__tests__')}`),
    )
    const offenders = files
      .filter((path) => readFileSync(path, 'utf-8').includes('System One'))
      .map((path) => relative(SRC_ROOT, path))
    expect(offenders).toEqual([])
  })
})
