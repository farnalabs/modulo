<template>
  <FeatureGate feature-name="eval_system" required-tier="community" show-disabled>

    <div class="page-wide">
    <PageHeader :title="$t('views.EvalEditorView.eval_editor')" :subtitle="$t('views.EvalEditorView.create_and_manage_eval_definitions')" />

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="pageError" :message="pageError" :on-retry="loadAll" />

    <template v-else>
      <div class="grid gap-6 lg:grid-cols-2">
        <div>
          <label for="evaleditorview-field-8" class="mb-1.5 block text-sm font-medium">{{ $t('views.EvalEditorView.pipeline') }}</label>
          <Select
  :aria-label="$t('views.EvalEditorView.pipeline_aria')"
  v-model="selectedPipelineId"
  @update:model-value="onPipelineChange"
  :placeholder="$t('views.EvalEditorView.select_a_pipeline')"
  data-testid="eval-editor-pipeline"
  class="w-full"
  :options="[{ value: '__all__', label: $t('common.none') }, ...pipelines.map(p => ({ value: p.id, label: p.name }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>

        <div>
          <label for="evaleditorview-field-7" class="mb-1.5 block text-sm font-medium">{{ $t('views.EvalEditorView.node') }} <span class="text-muted-foreground">({{ $t('views.EvalEditorView.node_optional') }})</span></label>
          <Select
  :aria-label="$t('views.EvalEditorView.node_aria')"
  v-model="form.node_id"
  :disabled="!selectedPipelineId || nodesLoading"
  :placeholder="$t('views.EvalEditorView.select_a_node')"
  data-testid="eval-editor-node"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.EvalEditorView.all_pipeline_outputs') }, ...nodes.map(n => ({ value: n.id, label: n.label || n.node_type || shortId(n.id) }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          <div v-if="nodesLoading" class="mt-1 text-xs text-muted-foreground">{{ $t('views.EvalEditorView.loading_nodes') }}</div>
          <ErrorAlert v-if="nodesError" :message="nodesError" :on-retry="loadNodes" compact class="mt-1" />
        </div>
      </div>

      <div class="grid gap-8 lg:grid-cols-5">
        <div class="lg:col-span-3">
          <div class="rounded-lg border bg-card p-6 shadow-sm">
            <h2 class="mb-4 text-base font-semibold">{{ editingEvalId ? $t('views.EvalEditorView.edit_eval') : $t('views.EvalEditorView.new_eval') }}</h2>

            <div class="space-y-4">
              <div>
                <label for="evaleditorview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.name') }}</label>
                <input id="evaleditorview-field-6"
                  v-model="form.name"
                  type="text"
                  data-testid="eval-editor-name"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  :placeholder="$t('views.EvalEditorView.name_placeholder')"
                />
              </div>

              <!-- ADR 066 D19: a two-axis Judging control — mechanism first,
                   then the backing. Never one flat list mixing a deterministic
                   eval_type with a backend. -->
              <div class="rounded-lg border bg-card p-4" data-testid="eval-editor-judging">
                <h3 class="mb-3 text-sm font-semibold">{{ $t('views.EvalEditorView.judging.heading') }}</h3>

                <div>
                  <label for="evaleditorview-mechanism" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.judging.mechanism') }}</label>
                  <Select
                    :aria-label="$t('views.EvalEditorView.judging.mechanismAria')"
                    :model-value="isTyped ? 'typed_questions' : 'deterministic'"
                    :placeholder="$t('views.EvalEditorView.judging.mechanism')"
                    data-testid="eval-editor-eval-type"
                    class="w-full"
                    :options="mechanismOptions"
                    option-label="label"
                    option-value="value"
                    @update:model-value="onMechanismChange"
                  >
                    <template #option="{ option }">
                      <span :data-value="option.value">{{ option.label }}</span>
                    </template>
                  </Select>
                </div>

                <template v-if="isTyped">
                  <div v-if="legacyMode" class="mt-3 rounded border border-amber-500/40 bg-amber-500/10 p-3 text-xs" data-testid="eval-editor-legacy-notice">
                    <p class="font-medium">{{ $t('views.EvalEditorView.judging.legacyNoticeTitle') }}</p>
                    <p class="mt-1 text-muted-foreground">{{ $t('views.EvalEditorView.judging.legacyNoticeBody') }}</p>
                    <pre class="mt-2 max-h-40 overflow-auto rounded bg-background p-2 font-mono text-[11px]">{{ legacyConfigJson }}</pre>
                    <button
                      type="button"
                      data-testid="eval-editor-migrate-legacy"
                      class="mt-2 rounded bg-primary px-2 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90"
                      @click="migrateLegacyConfig"
                    >
                      {{ $t('views.EvalEditorView.judging.migrateLegacy') }}
                    </button>
                  </div>

                  <div v-else class="mt-3 space-y-3" data-testid="eval-editor-config">
                    <div>
                      <label for="evaleditorview-backing-kind" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.judging.backing') }}</label>
                      <Select
                        :aria-label="$t('views.EvalEditorView.judging.backingAria')"
                        v-model="typedDraft.backingKind"
                        data-testid="eval-editor-backing-kind"
                        class="w-full"
                        :options="backingOptions"
                        option-label="label"
                        option-value="value"
                      >
                        <template #option="{ option }">
                          <span :data-value="option.value">{{ option.label }}</span>
                        </template>
                      </Select>
                    </div>

                    <div v-if="typedDraft.backingKind === 'model_backend'">
                      <label for="evaleditorview-backing-model" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.judging.modelBackend') }}</label>
                      <Select
                        :aria-label="$t('views.EvalEditorView.judging.modelBackendAria')"
                        v-model="typedDraft.modelBackendId"
                        data-testid="eval-editor-backing-model"
                        class="w-full"
                        :options="modelBackendOptions"
                        option-label="label"
                        option-value="value"
                      >
                        <template #option="{ option }">
                          <span :data-value="option.value">{{ option.label }}</span>
                        </template>
                      </Select>
                      <p v-if="modelBackendsError" class="mt-1 text-xs text-destructive">{{ modelBackendsError }}</p>
                      <p v-else-if="modelBackends.length === 0" class="mt-1 text-xs text-muted-foreground">{{ $t('views.EvalEditorView.judging.noModelBackends') }}</p>
                      <p v-else-if="!typedDraft.modelBackendId" class="mt-1 text-xs text-muted-foreground">{{ $t('views.EvalEditorView.judging.noBackendSelectedHint') }}</p>
                    </div>

                    <div v-else>
                      <label for="evaleditorview-backing-vendor" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.judging.decisionVendor') }}</label>
                      <input
                        id="evaleditorview-backing-vendor"
                        v-model="typedDraft.decisionVendor"
                        type="text"
                        data-testid="eval-editor-backing-vendor"
                        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        :placeholder="$t('views.EvalEditorView.judging.decisionVendorPlaceholder')"
                      />
                      <label for="evaleditorview-backing-model-class" class="mb-1 mt-2 block text-sm font-medium">{{ $t('views.EvalEditorView.judging.decisionModelClass') }}</label>
                      <input
                        id="evaleditorview-backing-model-class"
                        v-model="typedDraft.decisionModelClass"
                        type="text"
                        data-testid="eval-editor-backing-model-class"
                        class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        :placeholder="$t('views.EvalEditorView.judging.decisionModelClassPlaceholder')"
                      />
                    </div>

                    <p class="rounded bg-muted px-2 py-1 text-xs text-muted-foreground" data-testid="eval-editor-backing-preview">
                      {{ backingPreview }}
                    </p>

                    <div>
                      <label for="evaleditorview-verdict-kind" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.judging.verdictKind') }}</label>
                      <Select
                        :aria-label="$t('views.EvalEditorView.judging.verdictKindAria')"
                        v-model="typedDraft.verdictKind"
                        data-testid="eval-editor-verdict-kind"
                        class="w-full"
                        :options="verdictKindOptions"
                        option-label="label"
                        option-value="value"
                      >
                        <template #option="{ option }">
                          <span :data-value="option.value">{{ option.label }}</span>
                        </template>
                      </Select>
                    </div>
                    <div
                      v-if="typedDraft.verdictKind === 'derivation'"
                      role="alert"
                      aria-live="assertive"
                      data-testid="eval-editor-derivation-warning"
                      class="rounded border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive-text"
                    >
                      {{ $t('views.EvalEditorView.judging.derivationWarning') }}
                    </div>

                    <div>
                      <label for="evaleditorview-field" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.judging.judgedField') }}</label>
                      <input
                        id="evaleditorview-field"
                        v-model="typedDraft.field"
                        type="text"
                        data-testid="eval-editor-field"
                        :disabled="typedDraft.judgeWholeOutput"
                        class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                        :placeholder="$t('views.EvalEditorView.judging.judgedFieldPlaceholder')"
                      />
                      <label class="mt-2 flex items-center gap-2 text-xs">
                        <input
                          v-model="typedDraft.judgeWholeOutput"
                          type="checkbox"
                          data-testid="eval-editor-judge-whole-output"
                          class="accent-primary"
                        />
                        <span>{{ $t('views.EvalEditorView.judging.judgeWholeOutput') }}</span>
                      </label>
                    </div>

                    <div>
                      <label for="evaleditorview-context-fields" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.judging.contextFields') }}</label>
                      <input
                        id="evaleditorview-context-fields"
                        v-model="typedDraft.contextFields"
                        type="text"
                        data-testid="eval-editor-context-fields"
                        class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        :placeholder="$t('views.EvalEditorView.judging.contextFieldsPlaceholder')"
                      />
                    </div>

                    <div class="rounded border border-dashed p-3">
                      <div class="mb-2 flex items-center justify-between gap-2">
                        <h4 class="text-xs font-semibold uppercase tracking-wide">{{ $t('views.EvalEditorView.judging.questions') }}</h4>
                        <button
                          type="button"
                          data-testid="eval-editor-question-add"
                          class="rounded border border-input px-2 py-1 text-xs font-medium hover:bg-accent"
                          @click="addQuestion"
                        >
                          {{ $t('views.EvalEditorView.judging.addQuestion') }}
                        </button>
                      </div>

                      <div
                        v-for="(question, questionIndex) in typedDraft.questions"
                        :key="questionIndex"
                        data-testid="eval-editor-question"
                        class="mb-3 space-y-2 rounded border bg-background p-3 last:mb-0"
                      >
                        <div class="flex items-center justify-between gap-2">
                          <span class="text-xs font-medium">{{ $t('views.EvalEditorView.judging.questionNumber', { index: questionIndex + 1 }) }}</span>
                          <button
                            type="button"
                            data-testid="eval-editor-question-remove"
                            :aria-label="$t('views.EvalEditorView.judging.removeQuestionAria', { index: questionIndex + 1 })"
                            class="rounded p-1 text-destructive hover:bg-destructive/10"
                            @click="removeQuestion(questionIndex)"
                          >
                            <Trash2 class="h-3.5 w-3.5" aria-hidden="true" />
                          </button>
                        </div>

                        <div class="grid gap-2 sm:grid-cols-2">
                          <div>
                            <span class="mb-1 block text-xs font-medium">{{ $t('views.EvalEditorView.judging.questionId') }}</span>
                            <input
                              v-model="question.id"
                              type="text"
                              data-testid="eval-editor-question-id"
                              :aria-label="$t('views.EvalEditorView.judging.questionIdAria', { index: questionIndex + 1 })"
                              class="w-full rounded-lg border border-input bg-background px-2 py-1.5 font-mono text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            />
                          </div>
                          <div>
                            <span class="mb-1 block text-xs font-medium">{{ $t('views.EvalEditorView.judging.questionType') }}</span>
                            <Select
                              :aria-label="$t('views.EvalEditorView.judging.questionTypeAria', { index: questionIndex + 1 })"
                              :model-value="question.type"
                              data-testid="eval-editor-question-type"
                              class="w-full"
                              :options="questionTypeOptions"
                              option-label="label"
                              option-value="value"
                              @update:model-value="(value) => setQuestionType(question, value)"
                            >
                              <template #option="{ option }">
                                <span :data-value="option.value">{{ option.label }}</span>
                              </template>
                            </Select>
                          </div>
                        </div>

                        <div>
                          <span class="mb-1 block text-xs font-medium">{{ $t('views.EvalEditorView.judging.instructions') }}</span>
                          <textarea
                            v-model="question.instructions"
                            rows="2"
                            data-testid="eval-editor-question-instructions"
                            :aria-label="$t('views.EvalEditorView.judging.instructionsAria', { index: questionIndex + 1 })"
                            class="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          />
                        </div>

                        <div>
                          <span class="mb-1 block text-xs font-medium">{{ $t('views.EvalEditorView.judging.criteria') }}</span>
                          <textarea
                            v-model="question.criteriaText"
                            rows="2"
                            data-testid="eval-editor-question-criteria"
                            :aria-label="$t('views.EvalEditorView.judging.criteriaAria', { index: questionIndex + 1 })"
                            :placeholder="$t('views.EvalEditorView.judging.criteriaPlaceholder')"
                            class="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          />
                        </div>

                        <div class="grid gap-2 sm:grid-cols-3">
                          <div>
                            <span class="mb-1 block text-xs font-medium">{{ $t('views.EvalEditorView.judging.weight') }}</span>
                            <input
                              v-model="question.weight"
                              type="number"
                              min="0"
                              step="0.1"
                              data-testid="eval-editor-question-weight"
                              :aria-label="$t('views.EvalEditorView.judging.weightAria', { index: questionIndex + 1 })"
                              class="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            />
                          </div>
                          <div>
                            <span class="mb-1 block text-xs font-medium">{{ $t('views.EvalEditorView.judging.minPassConfidence') }}</span>
                            <input
                              v-model="question.minPassConfidence"
                              type="number"
                              min="0"
                              max="1"
                              step="0.05"
                              data-testid="eval-editor-question-min-confidence"
                              :aria-label="$t('views.EvalEditorView.judging.minPassConfidenceAria', { index: questionIndex + 1 })"
                              class="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            />
                          </div>
                          <label class="flex items-end gap-2 pb-1 text-xs">
                            <input
                              v-model="question.required"
                              type="checkbox"
                              data-testid="eval-editor-question-required"
                              :aria-label="$t('views.EvalEditorView.judging.requiredAria', { index: questionIndex + 1 })"
                              class="accent-primary"
                            />
                            <span>{{ $t('views.EvalEditorView.judging.required') }}</span>
                          </label>
                        </div>

                        <div v-if="question.type === 'choice'" class="rounded border border-dashed p-2">
                          <div class="mb-2 flex items-center justify-between gap-2">
                            <span class="text-xs font-medium">{{ $t('views.EvalEditorView.judging.options') }}</span>
                            <button
                              type="button"
                              data-testid="eval-editor-question-add-option"
                              :disabled="question.options.length >= maxChoiceOptions"
                              class="rounded border border-input px-2 py-0.5 text-xs font-medium hover:bg-accent disabled:opacity-50"
                              @click="addOption(question)"
                            >
                              {{ $t('views.EvalEditorView.judging.addOption') }}
                            </button>
                          </div>
                          <div
                            v-for="(option, optionIndex) in question.options"
                            :key="optionIndex"
                            class="mb-2 grid gap-2 sm:grid-cols-12"
                          >
                            <input
                              v-model="option.key"
                              type="text"
                              data-testid="eval-editor-question-option-key"
                              :aria-label="$t('views.EvalEditorView.judging.optionKeyAria', { question: questionIndex + 1, option: optionIndex + 1 })"
                              :placeholder="$t('views.EvalEditorView.judging.optionKey')"
                              class="rounded-lg border border-input bg-background px-2 py-1.5 font-mono text-xs sm:col-span-3"
                            />
                            <input
                              v-model="option.description"
                              type="text"
                              data-testid="eval-editor-question-option-description"
                              :aria-label="$t('views.EvalEditorView.judging.optionDescriptionAria', { question: questionIndex + 1, option: optionIndex + 1 })"
                              :placeholder="$t('views.EvalEditorView.judging.optionDescription')"
                              class="rounded-lg border border-input bg-background px-2 py-1.5 text-xs sm:col-span-4"
                            />
                            <label class="flex items-center gap-1 text-xs sm:col-span-2">
                              <input
                                v-model="option.passing"
                                type="checkbox"
                                data-testid="eval-editor-question-option-passing"
                                :aria-label="$t('views.EvalEditorView.judging.optionPassingAria', { question: questionIndex + 1, option: optionIndex + 1 })"
                                class="accent-primary"
                              />
                              <span>{{ $t('views.EvalEditorView.judging.passing') }}</span>
                            </label>
                            <input
                              v-model="option.score"
                              type="number"
                              min="0"
                              max="1"
                              step="0.05"
                              data-testid="eval-editor-question-option-score"
                              :aria-label="$t('views.EvalEditorView.judging.optionScoreAria', { question: questionIndex + 1, option: optionIndex + 1 })"
                              :placeholder="$t('views.EvalEditorView.judging.optionScore')"
                              class="rounded-lg border border-input bg-background px-2 py-1.5 text-xs sm:col-span-2"
                            />
                            <button
                              type="button"
                              data-testid="eval-editor-question-remove-option"
                              :aria-label="$t('views.EvalEditorView.judging.removeOptionAria', { question: questionIndex + 1, option: optionIndex + 1 })"
                              class="rounded p-1 text-destructive hover:bg-destructive/10 sm:col-span-1"
                              @click="removeOption(question, optionIndex)"
                            >
                              <Trash2 class="h-3.5 w-3.5" aria-hidden="true" />
                            </button>
                          </div>
                          <p class="text-[11px] text-muted-foreground">{{ $t('views.EvalEditorView.judging.optionsHint') }}</p>
                        </div>

                        <div v-if="question.type === 'score'" class="rounded border border-dashed p-2">
                          <div class="mb-2 flex items-center justify-between gap-2">
                            <span class="text-xs font-medium">{{ $t('views.EvalEditorView.judging.levels') }}</span>
                            <button
                              type="button"
                              data-testid="eval-editor-question-add-level"
                              :disabled="question.levels.length >= maxScoreLevels"
                              class="rounded border border-input px-2 py-0.5 text-xs font-medium hover:bg-accent disabled:opacity-50"
                              @click="addLevel(question)"
                            >
                              {{ $t('views.EvalEditorView.judging.addLevel') }}
                            </button>
                          </div>
                          <div
                            v-for="(level, levelIndex) in question.levels"
                            :key="levelIndex"
                            class="mb-2 grid gap-2 sm:grid-cols-12"
                          >
                            <input
                              v-model="level.score"
                              type="number"
                              data-testid="eval-editor-question-level-score"
                              :aria-label="$t('views.EvalEditorView.judging.levelScoreAria', { question: questionIndex + 1, level: levelIndex + 1 })"
                              :placeholder="$t('views.EvalEditorView.judging.levelScore')"
                              class="rounded-lg border border-input bg-background px-2 py-1.5 text-xs sm:col-span-3"
                            />
                            <input
                              v-model="level.description"
                              type="text"
                              data-testid="eval-editor-question-level-description"
                              :aria-label="$t('views.EvalEditorView.judging.levelDescriptionAria', { question: questionIndex + 1, level: levelIndex + 1 })"
                              :placeholder="$t('views.EvalEditorView.judging.levelDescription')"
                              class="rounded-lg border border-input bg-background px-2 py-1.5 text-xs sm:col-span-8"
                            />
                            <button
                              type="button"
                              data-testid="eval-editor-question-remove-level"
                              :aria-label="$t('views.EvalEditorView.judging.removeLevelAria', { question: questionIndex + 1, level: levelIndex + 1 })"
                              class="rounded p-1 text-destructive hover:bg-destructive/10 sm:col-span-1"
                              @click="removeLevel(question, levelIndex)"
                            >
                              <Trash2 class="h-3.5 w-3.5" aria-hidden="true" />
                            </button>
                          </div>
                          <div class="grid gap-2 sm:grid-cols-2">
                            <div>
                              <span class="mb-1 block text-xs font-medium">{{ $t('views.EvalEditorView.judging.passFrom') }}</span>
                              <input
                                v-model="question.passFrom"
                                type="number"
                                data-testid="eval-editor-question-pass-from"
                                :aria-label="$t('views.EvalEditorView.judging.passFromAria', { index: questionIndex + 1 })"
                                class="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-xs"
                              />
                            </div>
                          </div>
                          <p class="mt-1 text-[11px] text-muted-foreground">{{ $t('views.EvalEditorView.judging.levelsHint') }}</p>
                        </div>
                      </div>
                    </div>

                    <div class="grid gap-2 sm:grid-cols-3">
                      <div>
                        <label for="evaleditorview-pass-rule" class="mb-1 block text-xs font-medium">{{ $t('views.EvalEditorView.judging.passRuleThreshold') }}</label>
                        <input
                          id="evaleditorview-pass-rule"
                          v-model="typedDraft.threshold"
                          type="number"
                          min="0"
                          max="1"
                          step="0.05"
                          data-testid="eval-editor-pass-rule-threshold"
                          class="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        />
                      </div>
                      <div>
                        <label for="evaleditorview-timeout-question" class="mb-1 block text-xs font-medium">{{ $t('views.EvalEditorView.judging.perQuestionSeconds') }}</label>
                        <input
                          id="evaleditorview-timeout-question"
                          v-model="typedDraft.perQuestionSeconds"
                          type="number"
                          min="0"
                          step="1"
                          data-testid="eval-editor-timeout-question"
                          class="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        />
                      </div>
                      <div>
                        <label for="evaleditorview-timeout-definition" class="mb-1 block text-xs font-medium">{{ $t('views.EvalEditorView.judging.perDefinitionSeconds') }}</label>
                        <input
                          id="evaleditorview-timeout-definition"
                          v-model="typedDraft.perDefinitionSeconds"
                          type="number"
                          min="0"
                          step="1"
                          data-testid="eval-editor-timeout-definition"
                          class="w-full rounded-lg border border-input bg-background px-2 py-1.5 text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        />
                      </div>
                    </div>
                  </div>

                  <div
                    v-if="!legacyMode && clientErrors.length > 0"
                    aria-live="polite"
                    data-testid="eval-editor-client-errors"
                    class="mt-3 rounded border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive-text"
                  >
                    <ul class="list-disc space-y-0.5 pl-4">
                      <li v-for="(error, errorIndex) in clientErrors" :key="`${error.path}-${errorIndex}`">
                        {{ $t('views.EvalEditorView.judging.errors.' + error.key, error.params ?? {}) }}
                      </li>
                    </ul>
                  </div>
                </template>

                <template v-else>
                  <div class="mt-3">
                    <label for="evaleditorview-deterministic-type" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.judging.deterministicType') }}</label>
                    <Select
                      :aria-label="$t('views.EvalEditorView.judging.deterministicTypeAria')"
                      :model-value="deterministicType"
                      data-testid="eval-editor-deterministic-type"
                      class="w-full"
                      :options="deterministicOptions"
                      option-label="label"
                      option-value="value"
                      @update:model-value="onDeterministicTypeChange"
                    >
                      <template #option="{ option }">
                        <span :data-value="option.value">{{ option.label }}</span>
                      </template>
                    </Select>
                  </div>

                  <div class="mt-3">
                    <label for="evaleditorview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.config_json') }} <span class="text-muted-foreground">{{ $t('views.EvalEditorView.config_json_hint') }}</span></label>
                    <textarea id="evaleditorview-field-4"
                      v-model="form.config_json"
                      rows="6"
                      data-testid="eval-editor-config"
                      class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      :placeholder="configPlaceholder"
                    />
                    <div v-if="configParseError" class="mt-1 text-xs text-destructive">{{ configParseError }}</div>
                  </div>
                </template>

                <div
                  v-if="serverConfigError"
                  role="alert"
                  aria-live="assertive"
                  data-testid="eval-editor-server-error"
                  class="mt-3 rounded border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive-text"
                >
                  <p class="font-medium">{{ serverConfigErrorHeading }}</p>
                  <p class="mt-1">{{ serverConfigError.message }}</p>
                </div>
              </div>

              <div>
                <label for="evaleditorview-field-3" class="mb-1 block text-sm font-medium">
                  {{ $t('views.EvalEditorView.pass_threshold') }}
                  <span class="text-muted-foreground">({{ form.pass_threshold.toFixed(2) }})</span>
                </label>
                <div class="flex items-center gap-3">
                  <span class="text-xs text-muted-foreground">0.0</span>
                  <input id="evaleditorview-field-3"
                    v-model.number="form.pass_threshold"
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    data-testid="eval-editor-pass-threshold"
                    class="h-2 w-full cursor-pointer appearance-none rounded-full bg-input accent-primary"
                    :aria-label="$t('views.EvalEditorView.pass_threshold_aria')"
                  />
                  <span class="text-xs text-muted-foreground">1.0</span>
                </div>
                <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.EvalEditorView.pass_threshold_hint') }}</p>
              </div>

              <!-- Policy Gate section -->
              <div class="rounded-lg border border-dashed border-primary/30 bg-primary/5 p-4">
                <h3 class="mb-3 text-sm font-semibold" data-testid="policy-gate-heading">{{ $t('views.EvalEditorView.policyGate.heading') }}</h3>

                <div class="space-y-3">
                  <div class="flex items-center gap-4">
                    <label class="flex items-center gap-2 text-sm">
                      <input
                        type="radio"
                        value="warn"
                        v-model="policyGate.action"
                        data-testid="policy-gate-action-warn"
                        class="accent-primary"
                      />
                      <span>{{ $t('views.EvalEditorView.policyGate.actionWarnLabel') }}</span>
                    </label>
                    <label class="flex items-center gap-2 text-sm">
                      <input
                        type="radio"
                        value="block"
                        v-model="policyGate.action"
                        data-testid="policy-gate-action-block"
                        class="accent-primary"
                      />
                      <span>{{ $t('views.EvalEditorView.policyGate.actionBlockLabel') }}</span>
                    </label>
                  </div>

                  <p class="text-xs text-muted-foreground">
                    {{ policyGate.action === 'block'
                      ? $t('views.EvalEditorView.policyGate.actionBlockDescription')
                      : $t('views.EvalEditorView.policyGate.actionWarnDescription')
                    }}
                  </p>

                  <!-- Operator enable/disable toggle (CO-5) -->
                  <div v-if="policyGate.exists" class="flex items-center gap-2">
                    <button
                      type="button"
                      role="switch"
                      :aria-checked="policyGate.enabled"
                      :aria-label="$t('views.EvalEditorView.policyGate.toggleAriaLabel')"
                      data-testid="policy-gate-toggle"
                      class="inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors"
                      :class="policyGate.enabled ? 'bg-primary' : 'bg-input'"
                      :disabled="gateToggleBusy"
                      @click="requestToggleGate"
                    >
                      <span
                        class="pointer-events-none inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform"
                        :class="policyGate.enabled ? 'translate-x-4' : 'translate-x-0.5'"
                        aria-hidden="true"
                      />
                    </button>
                    <span class="text-xs text-muted-foreground">
                      {{ policyGate.enabled
                        ? $t('views.EvalEditorView.policyGate.enabledDescription')
                        : $t('views.EvalEditorView.policyGate.disabledDescription')
                      }}
                    </span>
                  </div>

                  <!-- Toggle disable confirmation (mirrors delete-confirmation pattern) -->
                  <dialog
                    v-if="gateToggleConfirming"
                    open
                    :aria-label="$t('views.EvalEditorView.policyGate.toggleDisableConfirm')"
                    data-testid="policy-gate-toggle-confirm-dialog"
                    class="static m-0 flex w-full items-center gap-2 rounded border border-destructive/30 bg-destructive/5 p-2 text-xs text-foreground"
                    @keydown="onGateToggleDialogKeydown"
                  >
                    <span>
                      {{ $t('views.EvalEditorView.policyGate.toggleDisableConfirm') }}
                      <template v-if="policyGate.action === 'block'">
                        {{ $t('views.EvalEditorView.policyGate.toggleDisableBlockWarning') }}
                      </template>
                    </span>
                    <button
                      type="button"
                      data-testid="policy-gate-confirm-toggle-disable"
                      class="rounded bg-destructive-fill px-2 py-0.5 text-xs font-medium text-destructive-foreground hover:bg-destructive-fill/90"
                      @click="confirmToggleGate"
                      ref="gateToggleConfirmBtnRef"
                    >
                      {{ $t('views.EvalEditorView.policyGate.toggleConfirmDisable') }}
                    </button>
                    <button
                      type="button"
                      data-testid="policy-gate-cancel-toggle-disable"
                      class="rounded px-2 py-0.5 text-xs font-medium hover:bg-accent"
                      @click="cancelToggleGate"
                    >
                      {{ $t('common.no') }}
                    </button>
                  </dialog>

                  <!-- Delete gate button -->
                  <div v-if="policyGate.exists" class="flex items-center gap-2">
                    <template v-if="!gateDeleteConfirming">
                      <button
                        type="button"
                        data-testid="policy-gate-delete"
                        ref="gateDeleteBtnRef"
                        :aria-label="$t('views.EvalEditorView.policyGate.deleteAriaLabel')"
                        class="inline-flex items-center gap-1 rounded border border-destructive/30 px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
                        @click="gateDeleteConfirming = true"
                      >
                        <Trash2 class="h-3 w-3" aria-hidden="true" />
                      </button>
                    </template>
                    <template v-else>
                      <dialog
                        open
                        :aria-label="$t('views.EvalEditorView.policyGate.deleteConfirm')"
                        data-testid="policy-gate-confirm-dialog"
                        class="static m-0 flex w-full items-center gap-2 rounded border border-destructive/30 bg-destructive/5 p-2 text-xs text-foreground"
                        @keydown="onGateDialogKeydown"
                      >
                        <span>
                          {{ $t('views.EvalEditorView.policyGate.deleteConfirm') }}
                          <template v-if="policyGate.action === 'block'">
                            {{ $t('views.EvalEditorView.policyGate.deleteConfirmBlockWarning') }}
                          </template>
                        </span>
                        <button
                          type="button"
                          data-testid="policy-gate-confirm-delete"
                          class="rounded bg-destructive-fill px-2 py-0.5 text-xs font-medium text-destructive-foreground hover:bg-destructive-fill/90"
                          @click="deletePolicyGate"
                          ref="gateDeleteConfirmBtnRef"
                        >
                          {{ $t('common.confirm') }}
                        </button>
                        <button
                          type="button"
                          data-testid="policy-gate-cancel-delete"
                          class="rounded px-2 py-0.5 text-xs font-medium hover:bg-accent"
                          @click="cancelGateDelete"
                        >
                          {{ $t('common.no') }}
                        </button>
                      </dialog>
                    </template>
                  </div>

                  <!-- Gate error state -->
                  <div
                    v-if="gateError"
                    role="alert"
                    aria-live="assertive"
                    data-testid="policy-gate-error"
                    class="flex items-center gap-2 rounded border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive-text"
                  >
                    <span>{{ $t('views.EvalEditorView.policyGate.errorState') }}</span>
                    <button
                      type="button"
                      data-testid="policy-gate-retry"
                      :aria-label="$t('views.EvalEditorView.policyGate.retryAriaLabel')"
                      class="rounded bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive-text hover:bg-destructive/20"
                      @click="retryPolicyGate"
                    >
                      {{ $t('views.EvalEditorView.policyGate.retry') }}
                    </button>
                  </div>
                </div>
              </div>

              <!-- Dirty-gate confirm dialog -->
              <dialog
                v-if="dirtyConfirmVisible"
                open
                :aria-label="$t('views.EvalEditorView.policyGate.dirtyConfirmAriaLabel')"
                data-testid="dirty-confirm-dialog"
                class="static m-0 flex w-full items-center gap-2 rounded border border-destructive/30 bg-destructive/5 p-2 text-xs text-foreground"
                @keydown="onDirtyDialogKeydown"
              >
                <span>{{ $t('views.EvalEditorView.policyGate.unsavedChangesConfirm') }}</span>
                <button
                  type="button"
                  data-testid="dirty-confirm-proceed"
                  class="rounded bg-destructive-fill px-2 py-0.5 text-xs font-medium text-destructive-foreground hover:bg-destructive-fill/90"
                  @click="resolveDirtyConfirm(true)"
                  ref="dirtyConfirmProceedBtnRef"
                >
                  {{ $t('views.EvalEditorView.policyGate.dirtyConfirmProceed') }}
                </button>
                <button
                  type="button"
                  data-testid="dirty-confirm-stay"
                  class="rounded px-2 py-0.5 text-xs font-medium hover:bg-accent"
                  @click="resolveDirtyConfirm(false)"
                >
                  {{ $t('views.EvalEditorView.policyGate.dirtyConfirmStay') }}
                </button>
              </dialog>

              <div class="flex items-center gap-2 pt-2">
              <Button :disabled="!canSave || saving" data-testid="eval-editor-save" @click="saveEval">
                {{ saving ? $t('common.saving') : editingEvalId ? $t('views.EvalEditorView.update') : $t('common.save') }}
              </Button>
                <button
                  type="button"
                  v-if="editingEvalId"
                  data-testid="eval-editor-cancel"
                  class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                  @click="handleCancel"
                >
                  {{ $t('common.cancel') }}
                </button>
              </div>

              <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
              <div v-if="formSuccess" class="text-sm text-success-text">{{ formSuccess }}</div>
            </div>
          </div>
        </div>

        <div class="lg:col-span-2">
          <h2 class="mb-4 text-base font-semibold">{{ $t('views.EvalEditorView.existing_evals') }}</h2>

          <ErrorAlert v-if="evalsError" :message="evalsError" :on-retry="loadEvals" compact class="mb-2" />

          <div v-if="!selectedPipelineId" class="rounded-lg border bg-card p-6 text-center text-sm text-muted-foreground">
            {{ $t('views.EvalEditorView.prompt_select_pipeline') }}
          </div>

          <div v-else-if="evalsLoading" class="space-y-2" data-testid="eval-editor-loading-skeleton">
            <div v-for="n in 3" :key="`eval-editor-skeleton-${n}`" class="rounded-lg border bg-card p-4">
              <SkeletonBlock height-class="h-5 w-1/3" />
              <SkeletonBlock class="mt-2" height-class="h-4 w-2/3" />
            </div>
          </div>

          <EmptyState
            v-else-if="evals.length === 0"
            :title="$t('views.EvalEditorView.no_evals_yet')"
          />

          <div v-else class="space-y-2">
            <div
              v-for="ev in evals"
              :key="ev.id"
              class="rounded-lg border bg-card p-4 shadow-sm"
            >
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0 flex-1">
                  <p class="truncate font-medium">{{ ev.name }}</p>
                  <div class="mt-1 flex flex-wrap items-center gap-2">
                    <span class="inline-block rounded bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary-text">{{ ev.eval_type === 'model_judge' ? $t('views.EvalEditorView.judging.mechanismTyped') : ev.eval_type }}</span>
                    <!-- ADR 066 §0: the judge display is composed by the backend
                         naming module and rendered verbatim — never composed here. -->
                    <span
                      v-if="ev.judge?.display"
                      class="inline-block rounded bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                      data-testid="eval-editor-judge-display"
                    >{{ ev.judge.display }}</span>
                    <span
                      v-if="evalGateActions[ev.id]"
                      class="inline-block rounded px-2 py-0.5 text-xs font-medium"
                      :class="evalGateActions[ev.id] === 'block' ? 'bg-destructive/10 text-destructive-text' : 'bg-amber-500/10 text-amber-700 dark:text-amber-400'"
                      data-testid="policy-gate-badge"
                    >
                      {{ evalGateActions[ev.id] === 'block' ? $t('views.EvalEditorView.policyGate.badgeBlock') : $t('views.EvalEditorView.policyGate.badgeWarn') }}
                    </span>
                    <span v-if="ev.pass_threshold != null" class="text-xs text-muted-foreground">
                      {{ $t('views.EvalEditorView.threshold', { value: ev.pass_threshold.toFixed(2) }) }}
                    </span>
                    <span v-if="ev.node_id" class="text-xs text-muted-foreground font-mono">
                      {{ $t('views.EvalEditorView.node_prefix', { id: shortId(ev.node_id) }) }}
                    </span>
                  </div>
                </div>
                <div class="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    data-testid="eval-editor-edit"
                    :aria-label="$t('common.edit')"
                    class="rounded p-1 text-muted-foreground hover:bg-accent"
                    :title="$t('common.edit')"
                    @click="startEdit(ev)"
                  >
                    <Pencil class="h-4 w-4" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    v-if="deletingEvalId !== ev.id"
                    data-testid="eval-editor-delete"
                    :aria-label="$t('common.delete')"
                    class="rounded p-1 text-destructive hover:bg-destructive/10"
                    :title="$t('common.delete')"
                    @click="confirmDelete(ev.id)"
                  >
                    <Trash2 class="h-4 w-4" aria-hidden="true" />
                  </button>
                  <div v-else class="flex items-center gap-1">
                    <button
                      type="button"
                      :disabled="deleting"
                      data-testid="eval-editor-confirm-delete"
                      class="rounded bg-destructive-fill px-2 py-1 text-xs font-medium text-destructive-foreground hover:bg-destructive-fill/90 disabled:opacity-50"
                      @click="deleteEval(ev.id)"
                    >
                      {{ deleting ? $t('common.deleting') : $t('common.confirm') }}
                    </button>
                    <button
                      type="button"
                      data-testid="eval-editor-cancel-delete"
                      class="rounded px-2 py-1 text-xs font-medium hover:bg-accent"
                      @click="deletingEvalId = null"
                    >
                      {{ $t('common.no') }}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, nextTick, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { shortId } from '../utils/format'
import { formatApiError } from '../lib/api/formatError'
import { useProductAnalytics } from '../composables/useProductAnalytics'
import { ANALYTICS_EVENTS } from '../composables/productAnalyticsEvents'
import EmptyState from '../components/shared/EmptyState.vue'
import SkeletonBlock from '../components/shared/SkeletonBlock.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import Button from 'primevue/button'
import Select from '../components/shared/AppSelect.vue'
import { Pencil, Trash2 } from '@lucide/vue'
import { api } from '../lib/api/client'
import {
  MAX_CHOICE_OPTIONS,
  MAX_SCORE_LEVELS,
  buildTypedJudgeConfig,
  createDefaultDraft,
  createQuestion,
  draftFromConfig,
  seedAnswerSpace,
  validateTypedJudgeDraft,
  type QuestionDraft,
  type QuestionType,
  type TypedJudgeDraft,
} from '../components/eval/typedJudge'

const { t } = useI18n()

const planStore = usePlanStore()
const { track } = useProductAnalytics()

interface PipelineItem {
  id: string
  name: string
  description: string | null
}

interface GraphNode {
  id: string
  node_type: string
  label?: string | null
  agent_id?: string | null
  position: { x: number; y: number }
}

interface EvalJudgeDisplay {
  display?: string
}

interface EvalDefinition {
  id: string
  pipeline_id: string
  node_id: string | null
  name: string
  eval_type: string
  config_json: Record<string, unknown>
  pass_threshold?: number | null
  suite_id?: string | null
  created_by: string
  judge?: EvalJudgeDisplay | null
}

interface ModelBackendItem {
  id: string
  name: string
  display_name: string
  provider: string
  model_id: string
}

type DeterministicType = 'regex' | 'json_schema' | 'custom_function'

const DETERMINISTIC_TYPE_OPTIONS: DeterministicType[] = ['regex', 'json_schema', 'custom_function']
const DETERMINISTIC_TYPES = new Set<DeterministicType>(DETERMINISTIC_TYPE_OPTIONS)

// A deterministic config is still authored as JSON, so each type gets a valid
// starting document instead of a placeholder the user must retype.
const DETERMINISTIC_DEFAULTS: Record<DeterministicType, string> = {
  regex: JSON.stringify({ pattern: '(error|failed)', field: 'output' }, null, 2),
  json_schema: JSON.stringify(
    { schema: { type: 'object', properties: { valid: { type: 'boolean' } } } },
    null,
    2,
  ),
  custom_function: JSON.stringify(
    { function: "def evaluate(output):\n    return output.get('score', 0) >= 0.8" },
    null,
    2,
  ),
}

// Stable 422 codes the judge write chokepoint emits (ADR 066 D4/D8).
const JUDGE_WRITE_CODES = new Set([
  'judge_config_invalid',
  'judge_config_typed_required',
  'judge_config_legacy_read_only',
  'judge_verdict_kind_unsupported',
  'judge_backend_not_found',
  'judge_backend_not_org_owned',
  'judge_backend_not_active',
])

const selectedPipelineId = ref('__all__')
const nodes = ref<GraphNode[]>([])
const nodesLoading = ref(false)

const nodesError = ref<string | null>(null)
const evalsError = ref<string | null>(null)

const form = reactive({
  name: '',
  node_id: '__all__',
  eval_type: 'model_judge',
  config_json: '',
  pass_threshold: 0.8,
})

// Typed-question authoring state (ADR 066 D4/D19). The builder owns the
// config; `form.config_json` is only the deterministic JSON editor's buffer.
const typedDraft = ref<TypedJudgeDraft>(createDefaultDraft())
const deterministicType = ref<DeterministicType>('regex')
// Authored deterministic JSON is kept per type so switching mechanism or type
// is a pause, not a silent discard (STATE-4).
const deterministicDrafts = ref<Record<DeterministicType, string>>({
  ...DETERMINISTIC_DEFAULTS,
})
// A legacy model_judge config is read-only until the author explicitly
// migrates it (ADR 066 D8) — a metadata edit must never silently rewrite it.
const legacyMode = ref(false)
const legacyConfigJson = ref<string | null>(null)
const serverConfigError = ref<{ code: string; message: string } | null>(null)
const modelBackends = ref<ModelBackendItem[]>([])
const modelBackendsError = ref<string | null>(null)

const saving = ref(false)
const formError = ref<string | null>(null)
const formSuccess = ref<string | null>(null)

const editingEvalId = ref<string | null>(null)

const evals = ref<EvalDefinition[]>([])
const evalsLoading = ref(false)

const deletingEvalId = ref<string | null>(null)
const deleting = ref(false)

// Policy Gate state (§3.2)
const policyGate = reactive({
  action: 'warn' as 'warn' | 'block',
  exists: false,
  id: null as string | null,
  version: 1,
  enabled: true as boolean,
  enabledAt: null as string | null,
  disabledAt: null as string | null,
})
const gateSnapshot = reactive({
  action: 'warn' as 'warn' | 'block',
})
const gateError = ref(false)
// Eval id whose gate write is pending a retry (set when phase 2 fails after a
// create, where editingEvalId is still null — §3.3 retry mechanics).
const gatePendingEvalId = ref<string | null>(null)
const gateDeleteConfirming = ref(false)
const gateDeleteBtnRef = ref<HTMLElement | null>(null)
const gateDeleteConfirmBtnRef = ref<HTMLElement | null>(null)
const evalGateActions = ref<Record<string, string>>({})

// Operator enable/disable toggle state (CO-5)
const gateToggleBusy = ref(false)
const gateToggleConfirming = ref(false)
const gateToggleConfirmBtnRef = ref<HTMLElement | null>(null)

// §3.2 dirty-gate confirm dialog state
const dirtyConfirmVisible = ref(false)
const dirtyConfirmProceedBtnRef = ref<HTMLElement | null>(null)
let dirtyConfirmResolve: ((value: boolean) => void) | null = null

const isTyped = computed(() => form.eval_type === 'model_judge')

const mechanismOptions = computed(() => [
  { value: 'typed_questions', label: t('views.EvalEditorView.judging.mechanismTyped') },
  { value: 'deterministic', label: t('views.EvalEditorView.judging.mechanismDeterministic') },
])

const deterministicOptions = computed(() =>
  DETERMINISTIC_TYPE_OPTIONS.map((value) => ({
    value,
    label: t(`views.EvalEditorView.${value}`),
  })),
)

const backingOptions = computed(() => [
  { value: 'model_backend', label: t('views.EvalEditorView.judging.backingModelBackend') },
  { value: 'decision_model', label: t('views.EvalEditorView.judging.backingDecisionModel') },
])

const verdictKindOptions = computed(() => [
  { value: 'assessment', label: t('views.EvalEditorView.judging.verdictAssessment') },
  { value: 'derivation', label: t('views.EvalEditorView.judging.verdictDerivation') },
])

const questionTypeOptions = computed(() => [
  { value: 'noul', label: t('views.EvalEditorView.judging.questionTypeNoul') },
  { value: 'choice', label: t('views.EvalEditorView.judging.questionTypeChoice') },
  { value: 'score', label: t('views.EvalEditorView.judging.questionTypeScore') },
])

const modelBackendOptions = computed(() => [
  { value: '', label: t('views.EvalEditorView.judging.noBackendSelected') },
  ...modelBackends.value.map((backend) => ({
    value: backend.id,
    label: `${backend.display_name ?? backend.name} — ${backend.provider} · ${backend.model_id}`,
  })),
])

const selectedModelBackend = computed(
  () => modelBackends.value.find((backend) => backend.id === typedDraft.value.modelBackendId) ?? null,
)

// ADR 066 §0: the mechanism label is "Typed questions" and a backing renders
// as `<mechanism> — <vendor/model> (<evidence>)`. The decision-model class
// phrase is read from its one locale value; this component never composes it,
// and a model-backed judge is always labelled "LLM, self-reported confidence".
const backingPreview = computed(() => {
  if (!isTyped.value) return ''
  const mechanism = t('views.EvalEditorView.judging.mechanismTyped')
  if (typedDraft.value.backingKind === 'decision_model') {
    const vendor = typedDraft.value.decisionVendor.trim() || t('views.EvalEditorView.judging.decisionModelFallback')
    return t('views.EvalEditorView.judging.displayDecisionModel', {
      mechanism,
      vendor,
      modelClass: t('views.EvalEditorView.judging.systemOneModel'),
    })
  }
  const backend = selectedModelBackend.value
  if (!backend) {
    if (typedDraft.value.modelBackendId) {
      return t('views.EvalEditorView.judging.displayModelBackendUnknown', {
        mechanism,
        id: typedDraft.value.modelBackendId,
      })
    }
    return t('views.EvalEditorView.judging.displayModelBackendUnset', { mechanism })
  }
  return t('views.EvalEditorView.judging.displayModelBackend', {
    mechanism,
    model: backend.model_id,
    vendor: backend.provider,
    evidence: t('views.EvalEditorView.judging.confidenceModelSelfReported'),
  })
})

const clientErrors = computed(() =>
  isTyped.value && !legacyMode.value ? validateTypedJudgeDraft(typedDraft.value) : [],
)

const serverConfigErrorHeading = computed(() => {
  const code = serverConfigError.value?.code
  if (code && JUDGE_WRITE_CODES.has(code)) {
    return t(`views.EvalEditorView.judging.serverErrors.${code}`)
  }
  return t('views.EvalEditorView.judging.serverErrors.generic')
})

const deterministicConfigValid = computed(() => {
  if (!form.config_json.trim()) return false
  try {
    JSON.parse(form.config_json)
    return true
  } catch {
    return false
  }
})

const configParseError = computed(() => {
  if (isTyped.value || !form.config_json.trim()) return null
  return deterministicConfigValid.value ? null : t('views.EvalEditorView.invalid_json')
})

const configPlaceholder = computed(() => {
  return t(`views.EvalEditorView.configPlaceholder.${form.eval_type || 'model_judge'}`)
})

const canSave = computed(() => {
  if (!selectedPipelineId.value || selectedPipelineId.value === '__all__') return false
  if (!form.name.trim() || !form.eval_type) return false
  if (isTyped.value) return legacyMode.value || clientErrors.value.length === 0
  return deterministicConfigValid.value
})

const maxChoiceOptions = MAX_CHOICE_OPTIONS
const maxScoreLevels = MAX_SCORE_LEVELS

function rememberDeterministicDraft(): void {
  deterministicDrafts.value[deterministicType.value] = form.config_json
}

function restoreDeterministicDraft(type: DeterministicType): string {
  return deterministicDrafts.value[type]
}

function onMechanismChange(value: unknown): void {
  const mechanism = String(value)
  const typed = mechanism === 'typed_questions'
  if (typed === isTyped.value) return
  if (typed) {
    rememberDeterministicDraft()
    form.eval_type = 'model_judge'
    return
  }
  form.eval_type = deterministicType.value
  form.config_json = restoreDeterministicDraft(deterministicType.value)
}

function onDeterministicTypeChange(value: unknown): void {
  const next = String(value) as DeterministicType
  if (!DETERMINISTIC_TYPES.has(next)) return
  if (next === deterministicType.value) return
  rememberDeterministicDraft()
  deterministicType.value = next
  form.eval_type = next
  form.config_json = restoreDeterministicDraft(next)
}

function setQuestionType(question: QuestionDraft, value: unknown): void {
  const next = String(value) as QuestionType
  if (next !== 'noul' && next !== 'choice' && next !== 'score') return
  question.type = next
  seedAnswerSpace(question)
}

function nextQuestionId(): string {
  let index = typedDraft.value.questions.length + 1
  while (typedDraft.value.questions.some((question) => question.id === `q${index}`)) {
    index += 1
  }
  return `q${index}`
}

function addQuestion(): void {
  typedDraft.value.questions.push(createQuestion('noul', nextQuestionId()))
}

function removeQuestion(index: number): void {
  typedDraft.value.questions.splice(index, 1)
}

function addOption(question: QuestionDraft): void {
  if (question.options.length >= MAX_CHOICE_OPTIONS) return
  question.options.push({ key: '', description: '', passing: false, score: '' })
}

function removeOption(question: QuestionDraft, index: number): void {
  question.options.splice(index, 1)
}

function addLevel(question: QuestionDraft): void {
  if (question.levels.length >= MAX_SCORE_LEVELS) return
  question.levels.push({ score: '', description: '' })
}

function removeLevel(question: QuestionDraft, index: number): void {
  question.levels.splice(index, 1)
}

function migrateLegacyConfig(): void {
  const draft = createDefaultDraft()
  // The migration action only renders while a legacy config is loaded, so the
  // buffer is non-null here; it is always the JSON.stringify of the stored row.
  const legacy = JSON.parse(legacyConfigJson.value as string) as Record<string, unknown>
  if (typeof legacy.field === 'string' && legacy.field.trim() !== '') {
    draft.field = legacy.field
  }
  typedDraft.value = draft
  legacyMode.value = false
  legacyConfigJson.value = null
}

async function loadModelBackends(): Promise<void> {
  modelBackendsError.value = null
  try {
    const { data, error } = await api.GET('/api/v1/model-backends', {
      params: { query: { page_size: 100 } },
    })
    if (error) {
      modelBackends.value = []
      modelBackendsError.value = t('views.EvalEditorView.judging.failedToLoadBackends')
      return
    }
    modelBackends.value = (data?.items ?? []) as ModelBackendItem[]
  } catch {
    modelBackends.value = []
    modelBackendsError.value = t('views.EvalEditorView.judging.failedToLoadBackends')
  }
}

// §3.2 dirty-gate guard: the gate is dirty when its action differs from the
// snapshot taken on edit-start (or from the default 'warn' when creating).
const isGateDirty = computed(() => policyGate.action !== gateSnapshot.action)

function resetForm() {
  form.name = ''
  form.node_id = '__all__'
  form.eval_type = 'model_judge'
  form.config_json = ''
  form.pass_threshold = 0.8
  typedDraft.value = createDefaultDraft()
  deterministicType.value = 'regex'
  deterministicDrafts.value = { ...DETERMINISTIC_DEFAULTS }
  legacyMode.value = false
  legacyConfigJson.value = null
  serverConfigError.value = null
  editingEvalId.value = null
  formError.value = null
  formSuccess.value = null
  // Reset gate state (§3.2 — every lifecycle transition)
  policyGate.action = 'warn'
  policyGate.exists = false
  policyGate.id = null
  policyGate.version = 1
  policyGate.enabled = true
  policyGate.enabledAt = null
  policyGate.disabledAt = null
  gateSnapshot.action = 'warn'
  gateError.value = false
  gateDeleteConfirming.value = false
  gateDeleteBtnRef.value = null
  gatePendingEvalId.value = null
  gateToggleBusy.value = false
  gateToggleConfirming.value = false
  // Reset dirty-gate confirm dialog (§3.2)
  dirtyConfirmVisible.value = false
  if (dirtyConfirmResolve) {
    dirtyConfirmResolve(false)
    dirtyConfirmResolve = null
  }
}

const { loading, error: pageError, data: pipelinesResp, load: loadAll } = useDataFetch(
  async () => {
    const { data } = await api.GET('/api/v1/pipelines')
    return { data: data?.items ?? [], error: undefined }
  },
  { initialValue: [] as PipelineItem[] },
)

const pipelines = computed(() => (pipelinesResp.value ?? []) as PipelineItem[])

async function loadNodes() {
  if (!selectedPipelineId.value || selectedPipelineId.value === '__all__') {
    nodes.value = []
    return
  }
  nodesLoading.value = true
  nodesError.value = null
  try {
    const { data } = await api.GET('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: selectedPipelineId.value } },
    })
    nodes.value = data?.nodes ?? []
  } catch (e) {
    nodes.value = []
    nodesError.value = t('views.EvalEditorView.failed_to_load_nodes')
    console.warn('Failed to load nodes:', e)
  } finally {
    nodesLoading.value = false
  }
}

async function loadEvals() {
  if (!selectedPipelineId.value || selectedPipelineId.value === '__all__') {
    evals.value = []
    return
  }
  evalsLoading.value = true
  evalsError.value = null
  try {
    const { data } = await api.GET('/api/v1/evals', {
      params: { query: { pipeline_id: selectedPipelineId.value } },
    })
    evals.value = data?.items ?? []
    // Fetch gate actions for badge display (§6.1 / criterion 16)
    await loadGateBadges()
  } catch {
    evals.value = []
    evalsError.value = t('views.EvalEditorView.failed_to_load_evals')
  } finally {
    evalsLoading.value = false
  }
}

async function loadGateBadges() {
  const actions: Record<string, string> = {}
  await Promise.all(
    evals.value.map(async (ev) => {
      try {
        const { data } = await api.GET('/api/v1/evals/{eval_id}/policy-gate', {
          params: { path: { eval_id: ev.id } },
        })
        if (data?.action) {
          actions[ev.id] = data.action
        }
      } catch {
        // 404 means no gate — skip silently
      }
    }),
  )
  evalGateActions.value = actions
}

async function fetchPolicyGate(evalId: string) {
  gateError.value = false
  gatePendingEvalId.value = null
  let gate: {
    action?: string
    id?: string
    version?: number
    enabled?: boolean
    enabled_at?: string | null
    disabled_at?: string | null
  } | null = null
  try {
    const res = await api.GET('/api/v1/evals/{eval_id}/policy-gate', {
      params: { path: { eval_id: evalId } },
    })
    // openapi-fetch resolves non-2xx as { data: undefined, error } — it never
    // throws, so the envelope error (404 etc.) must be handled here, not in a catch.
    gate = res.data ?? null
  } catch {
    // Network-level failure only — treat as "no gate known".
    gate = null
  }
  if (gate && typeof gate.action === 'string') {
    policyGate.action = gate.action as 'warn' | 'block'
    policyGate.exists = true
    policyGate.id = gate.id ?? null
    policyGate.version = gate.version ?? 1
    policyGate.enabled = gate.enabled ?? true
    policyGate.enabledAt = gate.enabled_at ?? null
    policyGate.disabledAt = gate.disabled_at ?? null
    gateSnapshot.action = gate.action as 'warn' | 'block'
  } else {
    // 404 / no gate exists — reset to defaults so a previously viewed eval's
    // gate identity never leaks into this one (§3.2, criteria 13/24).
    policyGate.action = 'warn'
    policyGate.exists = false
    policyGate.id = null
    policyGate.version = 1
    policyGate.enabled = true
    policyGate.enabledAt = null
    policyGate.disabledAt = null
    gateSnapshot.action = 'warn'
  }
}

async function onPipelineChange() {
  resetForm()
  deletingEvalId.value = null
  nodesError.value = null
  evalsError.value = null
  await Promise.all([loadNodes(), loadEvals()])
}

/**
 * The config this save will send.
 *
 * Typed: the builder's exact wire payload (ADR 066 D4). A legacy definition in
 * read-only mode re-sends its stored config unchanged, so a metadata edit does
 * not silently migrate it — migration is an explicit author action.
 */
function buildConfigForSave(): Record<string, unknown> {
  if (isTyped.value) {
    if (legacyMode.value && legacyConfigJson.value !== null) {
      return JSON.parse(legacyConfigJson.value) as Record<string, unknown>
    }
    return buildTypedJudgeConfig(typedDraft.value)
  }
  return JSON.parse(form.config_json) as Record<string, unknown>
}

function buildEvalBody(configParsed: Record<string, unknown>) {
  return {
    pipeline_id: selectedPipelineId.value,
    node_id: form.node_id === '__all__' ? null : form.node_id,
    name: form.name.trim(),
    eval_type: form.eval_type,
    config_json: configParsed,
    pass_threshold: form.pass_threshold,
  }
}

// Phase 1 result: `ok: false` means the write failed (formError is set);
// `evalId: null` means the create succeeded but the response carried no id —
// the original flow still reported success in that case, so the two must stay
// distinguishable.
type PersistEvalResult = { ok: true; evalId: string | null } | { ok: false }

/**
 * Surface a failed eval write. The judge chokepoint's typed 422 codes
 * (``judge_config_*`` / ``judge_backend_*``) render inline next to the builder
 * — the server stays authoritative and the client mirror cannot check backend
 * existence, ownership or activity.
 */
function handleEvalWriteError(error: unknown): void {
  const code =
    typeof (error as { code?: unknown } | null)?.code === 'string'
      ? (error as { code: string }).code
      : null
  if (code && code.startsWith('judge_')) {
    serverConfigError.value = { code, message: formatApiError(error) }
    formError.value = null
    return
  }
  serverConfigError.value = null
  formError.value = formatApiError(error)
}

// Phase 1: persist the eval definition itself.
async function persistEval(configParsed: Record<string, unknown>): Promise<PersistEvalResult> {
  try {
    const evalId = editingEvalId.value
    if (evalId) {
      const res = await api.PUT('/api/v1/evals/{eval_id}', {
        params: { path: { eval_id: evalId } },
        body: buildEvalBody(configParsed),
      })
      // openapi-fetch resolves non-2xx as { data: undefined, error } — it never
      // throws, so the envelope error must be handled here.
      if (res.error) {
        handleEvalWriteError(res.error)
        return { ok: false }
      }
      return { ok: true, evalId }
    }
    const { data: created, error } = await api.POST('/api/v1/evals', {
      body: buildEvalBody(configParsed),
    })
    if (error) {
      handleEvalWriteError(error)
      return { ok: false }
    }
    const createdId = typeof created?.id === 'string' ? created.id : null
    if (createdId) {
      track(ANALYTICS_EVENTS.EVAL_CREATED, { eval_id: createdId })
    }
    return { ok: true, evalId: createdId }
  } catch (e: unknown) {
    formError.value = formatApiError(e)
    return { ok: false }
  }
}

// Phase 2a: update the existing policy gate. Returns false on an envelope error.
async function updatePolicyGate(evalId: string): Promise<boolean> {
  const res = await api.PUT('/api/v1/evals/{eval_id}/policy-gate', {
    params: { path: { eval_id: evalId } },
    body: { action: policyGate.action },
  })
  // openapi-fetch resolves non-2xx as { data: undefined, error } — it never
  // throws, so the envelope error must be checked here.
  if (res.error || !res.data) return false
  const d = res.data
  policyGate.id = d.id ?? policyGate.id
  policyGate.version = d.version ?? policyGate.version + 1
  gateSnapshot.action = policyGate.action
  return true
}

// Phase 2b: create the policy gate. Returns false on an envelope error.
async function createPolicyGate(evalId: string): Promise<boolean> {
  const res = await api.POST('/api/v1/evals/{eval_id}/policy-gate', {
    params: { path: { eval_id: evalId } },
    body: { action: policyGate.action },
  })
  if (res.error || !res.data) return false
  const d = res.data
  policyGate.id = d.id ?? null
  policyGate.exists = true
  policyGate.version = d.version ?? 1
  gateSnapshot.action = policyGate.action
  return true
}

// Phase 2: save the policy gate (if modified or new). Two-phase reporting
// (§3.3): the eval half reports its own success even when the gate half fails,
// and the form is NOT reset so the retry stays available.
async function persistPolicyGateIfModified(savedEvalId: string): Promise<boolean> {
  const gateModified = policyGate.action !== gateSnapshot.action || !policyGate.exists
  if (!gateModified) return true
  try {
    if (policyGate.exists && policyGate.id) return await updatePolicyGate(savedEvalId)
    return await createPolicyGate(savedEvalId)
  } catch {
    // Network-level failure — same two-phase reporting path.
    return false
  }
}

async function saveEval() {
  if (!canSave.value) return

  // Capture before any state mutation: on the create path editingEvalId is
  // null, and savedEvalId (the new id) must not be read as "was editing".
  const wasEditing = editingEvalId.value !== null
  saving.value = true
  formError.value = null
  formSuccess.value = null
  serverConfigError.value = null
  gateError.value = false

  let configParsed: Record<string, unknown> = {}
  try {
    configParsed = buildConfigForSave()
  } catch {
    formError.value = t('views.EvalEditorView.config_json_is_invalid')
    saving.value = false
    return
  }

  const persisted = await persistEval(configParsed)
  if (!persisted.ok) {
    saving.value = false
    return
  }

  // Phase 2 is skipped when the create returned no id (nothing to attach the
  // gate to) — the eval half still reports success, as before.
  const gateSaved = persisted.evalId
    ? await persistPolicyGateIfModified(persisted.evalId)
    : true
  if (!gateSaved) {
    gateError.value = true
    gatePendingEvalId.value = persisted.evalId
    formSuccess.value = wasEditing
      ? t('views.EvalEditorView.eval_updated')
      : t('views.EvalEditorView.eval_created')
    await loadEvals()
    saving.value = false
    return
  }

  // Reset and show success
  resetForm()
  formSuccess.value = wasEditing
    ? t('views.EvalEditorView.eval_updated')
    : t('views.EvalEditorView.eval_created')
  await loadEvals()
  saving.value = false
}

async function startEdit(ev: EvalDefinition) {
  // §3.2 dirty-gate guard: prompt when switching evals with unsaved gate changes
  if (editingEvalId.value !== null && !(await confirmGateDirty())) return
  editingEvalId.value = ev.id
  form.name = ev.name
  form.node_id = ev.node_id ?? '__all__'
  form.eval_type = ev.eval_type
  form.pass_threshold = ev.pass_threshold ?? 0.8
  formError.value = null
  formSuccess.value = null
  serverConfigError.value = null
  legacyMode.value = false
  legacyConfigJson.value = null
  deterministicDrafts.value = { ...DETERMINISTIC_DEFAULTS }

  if (ev.eval_type === 'model_judge') {
    const draft = draftFromConfig(ev.config_json)
    if (draft) {
      typedDraft.value = draft
      form.config_json = ''
    } else {
      // ADR 066 D8: a stored legacy prompt blob is readable but immutable.
      // Editing its config must migrate it, so the author gets an explicit
      // migration action instead of a silent rewrite on save.
      legacyMode.value = true
      legacyConfigJson.value = JSON.stringify(ev.config_json, null, 2)
      typedDraft.value = createDefaultDraft()
      form.config_json = ''
    }
  } else {
    deterministicType.value = DETERMINISTIC_TYPES.has(ev.eval_type as DeterministicType)
      ? (ev.eval_type as DeterministicType)
      : 'regex'
    form.config_json = JSON.stringify(ev.config_json, null, 2)
    deterministicDrafts.value = {
      ...DETERMINISTIC_DEFAULTS,
      [deterministicType.value]: form.config_json,
    }
    typedDraft.value = createDefaultDraft()
  }
  // Fetch gate state for this eval (§3.2 — switching evals re-populates)
  fetchPolicyGate(ev.id)
}

function confirmDelete(id: string) {
  deletingEvalId.value = id
  deleting.value = false
}

async function deleteEval(id: string) {
  deleting.value = true
  try {
    await api.DELETE('/api/v1/evals/{eval_id}', {
      params: { path: { eval_id: id } },
    })
    evals.value = evals.value.filter(e => e.id !== id)
    deletingEvalId.value = null
  } catch (e: unknown) {
    const errMsg = formatApiError(e)
    if (errMsg.toLowerCase().includes('not found') || errMsg.includes('404')) {
      formError.value = t('views.EvalEditorView.eval_already_deleted')
    } else {
      formError.value = errMsg
    }
  } finally {
    deleting.value = false
  }
}

// These confirmation strips are inline (non-modal) elements inside the form:
// Escape cancels them, but Tab stays in the document's natural order — a Tab
// trap would strand keyboard users in a two-button strip. aria-modal is
// likewise omitted because the form behind the strip is not inert.
function onGateDialogKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    cancelGateDelete()
  }
}

// §3.3 auto-focus the confirm button when the delete-confirmation dialog opens
watch(gateDeleteConfirming, (confirming) => {
  if (confirming) {
    nextTick(() => gateDeleteConfirmBtnRef.value?.focus())
  }
})

// §3.2 auto-focus the proceed button when the dirty-confirm dialog opens
watch(dirtyConfirmVisible, (visible) => {
  if (visible) {
    nextTick(() => dirtyConfirmProceedBtnRef.value?.focus())
  }
})

// §3.2 dirty-gate guard: returns true if the gate is dirty and the user should
// be prompted before discarding. Returns false if not dirty (proceed freely).
async function confirmGateDirty(): Promise<boolean> {
  if (!isGateDirty.value) return true
  return new Promise<boolean>((resolve) => {
    dirtyConfirmResolve = resolve
    dirtyConfirmVisible.value = true
  })
}

function resolveDirtyConfirm(proceed: boolean) {
  dirtyConfirmVisible.value = false
  if (dirtyConfirmResolve) {
    dirtyConfirmResolve(proceed)
    dirtyConfirmResolve = null
  }
}

// Non-modal inline strip: Escape only (no Tab trap — see onGateDialogKeydown).
function onDirtyDialogKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    resolveDirtyConfirm(false)
  }
}

async function handleCancel() {
  if (!(await confirmGateDirty())) return
  resetForm()
}

function cancelGateDelete() {
  gateDeleteConfirming.value = false
  nextTick(() => {
    gateDeleteBtnRef.value?.focus()
  })
}

async function deletePolicyGate() {
  if (!editingEvalId.value) return
  gateDeleteConfirming.value = false
  try {
    const res = await api.DELETE('/api/v1/evals/{eval_id}/policy-gate', {
      params: { path: { eval_id: editingEvalId.value } },
    })
    // openapi-fetch resolves non-2xx as { data: undefined, error } without
    // throwing — surface the envelope error so the 404 branch below runs.
    if (res.error) throw res.error
    policyGate.exists = false
    policyGate.id = null
    policyGate.action = 'warn'
    policyGate.version = 1
    policyGate.enabled = true
    policyGate.enabledAt = null
    policyGate.disabledAt = null
    gateSnapshot.action = 'warn'
    gateError.value = false
    // Refresh badges
    await loadGateBadges()
  } catch (e: unknown) {
    const errMsg = formatApiError(e)
    if (errMsg.toLowerCase().includes('not found') || errMsg.includes('404')) {
      // Gate already deleted
      policyGate.exists = false
      policyGate.id = null
      policyGate.action = 'warn'
      policyGate.version = 1
      policyGate.enabled = true
      policyGate.enabledAt = null
      policyGate.disabledAt = null
      gateSnapshot.action = 'warn'
    } else {
      formError.value = errMsg
    }
  }
}

// ---------------------------------------------------------------------------
// Operator enable/disable toggle (CO-5)
// ---------------------------------------------------------------------------

function requestToggleGate() {
  if (!policyGate.exists || gateToggleBusy.value) return
  // Enabling: toggle immediately. Disabling a block gate: confirm first.
  if (!policyGate.enabled) {
    doToggleGate(true)
  } else if (policyGate.action === 'block') {
    gateToggleConfirming.value = true
  } else {
    doToggleGate(false)
  }
}

async function doToggleGate(enabled: boolean) {
  if (!editingEvalId.value || !policyGate.id) return
  gateToggleConfirming.value = false
  gateToggleBusy.value = true
  try {
    const res = await api.PATCH('/api/v1/evals/{eval_id}/policy-gate/toggle', {
      params: { path: { eval_id: editingEvalId.value } },
      body: { enabled },
    })
    if (res.error || !res.data) {
      formError.value = t('views.EvalEditorView.policyGate.toggleError')
      return
    }
    const d = res.data as { enabled?: boolean; enabled_at?: string | null; disabled_at?: string | null }
    policyGate.enabled = d.enabled ?? enabled
    policyGate.enabledAt = d.enabled_at ?? null
    policyGate.disabledAt = d.disabled_at ?? null
  } catch {
    formError.value = t('views.EvalEditorView.policyGate.toggleError')
  } finally {
    gateToggleBusy.value = false
  }
}

function confirmToggleGate() {
  gateToggleConfirming.value = false
  doToggleGate(false)
}

function cancelToggleGate() {
  gateToggleConfirming.value = false
  nextTick(() => {
    gateToggleConfirmBtnRef.value?.focus()
  })
}

// Non-modal inline strip: Escape only (no Tab trap — see onGateDialogKeydown).
function onGateToggleDialogKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    cancelToggleGate()
  }
}

// Auto-focus the confirm button when the toggle-disable dialog opens
watch(gateToggleConfirming, (confirming) => {
  if (confirming) {
    nextTick(() => gateToggleConfirmBtnRef.value?.focus())
  }
})

async function retryPolicyGate() {
  // Retry target: the eval being edited, or — after a failed gate create on
  // the create path, where editingEvalId is still null — the eval id created
  // in phase 1 (§3.3).
  const evalId = editingEvalId.value ?? gatePendingEvalId.value
  if (!evalId) return
  try {
    // Always the update endpoint — even when the failed write was the create
    // (spec: the retry re-issues the same action via the update route).
    const res = await api.PUT('/api/v1/evals/{eval_id}/policy-gate', {
      params: { path: { eval_id: evalId } },
      body: { action: policyGate.action },
    })
    // openapi-fetch resolves non-2xx as { data: undefined, error } — a failed
    // retry keeps the error state available (§3.3 — no backoff/circuit breaker).
    if (res.error || !res.data) return
    const d = res.data
    policyGate.id = d.id ?? policyGate.id
    policyGate.exists = true
    policyGate.version = d.version ?? policyGate.version + 1
    policyGate.enabled = d.enabled ?? policyGate.enabled
    policyGate.enabledAt = d.enabled_at ?? policyGate.enabledAt
    policyGate.disabledAt = d.disabled_at ?? policyGate.disabledAt
    gateSnapshot.action = policyGate.action
    gateError.value = false
    gatePendingEvalId.value = null
    // §6.1 refresh the eval-list badge after a successful retry
    await loadGateBadges()
  } catch {
    // Network-level failure — keep error state available (§3.3)
  }
}

onMounted(() => {
  planStore.fetchPlan()
  loadAll()
  // The typed builder's backing axis needs the org's model backends. A failure
  // is non-fatal: the select stays empty and shows an inline message.
  loadModelBackends()
})
</script>
