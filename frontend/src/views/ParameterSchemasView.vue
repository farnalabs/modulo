<template>
  <div class="page-wide">
    <template v-if="!editingSchema">
      <header class="flex items-center justify-between">
        <PageHeader
          :title="$t('views.ParameterSchemasView.title')"
          :subtitle="$t('views.ParameterSchemasView.subtitle')"
        />
        <Button data-testid="paramschema-new" @click="startNewSchema">
          {{ $t('views.ParameterSchemasView.new_schema') }}
        </Button>
      </header>

      <LoadingSpinner v-if="loading" />
      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadSchemas" />

      <template v-else>
        <EmptyState
          v-if="schemas.length === 0"
          :title="$t('views.ParameterSchemasView.no_schemas')"
          :description="$t('views.ParameterSchemasView.no_schemas_hint')"
        />

        <div v-else class="overflow-x-auto rounded-lg border">
          <table class="w-full text-left text-sm">
            <thead class="bg-muted/50">
              <tr>
                <th class="px-4 py-3 font-medium">{{ $t('views.ParameterSchemasView.name') }}</th>
                <th class="px-4 py-3 font-medium">{{ $t('views.ParameterSchemasView.description') }}</th>
                <th class="px-4 py-3 font-medium">{{ $t('views.ParameterSchemasView.version') }}</th>
                <th class="px-4 py-3 font-medium">{{ $t('views.ParameterSchemasView.parameters') }}</th>
                <th class="px-4 py-3 font-medium text-right">{{ $t('views.ParameterSchemasView.actions') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr
                v-for="schema in schemas"
                :key="schema.id"
                class="hover:bg-muted/30 transition-colors cursor-pointer"
                @click="editSchema(schema)"
              >
                <td class="px-4 py-3 font-medium">{{ schema.name }}</td>
                <td class="px-4 py-3 text-muted-foreground">{{ schema.description || '—' }}</td>
                <td class="px-4 py-3">v{{ schema.version }}</td>
                <td class="px-4 py-3">{{ schema.parameters?.length ?? 0 }}</td>
                <td class="px-4 py-3 text-right">
                  <button
                    type="button"
                    class="rounded p-1 text-muted-foreground hover:bg-accent hover:text-destructive"
                    :aria-label="$t('views.ParameterSchemasView.delete')"
                    data-testid="paramschema-delete"
                    @click.stop="confirmDelete(schema)"
                  >
                    <Trash2 class="h-4 w-4" aria-hidden="true" />
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="deleteConfirmId" class="mt-4 rounded-lg border border-destructive/50 bg-destructive/10 p-4">
          <p class="text-sm font-medium text-destructive">
            {{ $t('views.ParameterSchemasView.delete_confirm', { name: deleteConfirmName }) }}
          </p>
          <p v-if="deleteRefError" class="mt-1 text-sm text-destructive/80">{{ deleteRefError }}</p>
          <div class="mt-3 flex items-center gap-2">
            <Button :disabled="deleting" severity="danger" @click="doDelete">
              {{ deleting ? $t('views.ParameterSchemasView.deleting') : $t('views.ParameterSchemasView.delete') }}
            </Button>
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
              @click="deleteConfirmId = null"
            >
              {{ $t('views.ParameterSchemasView.cancel') }}
            </button>
          </div>
        </div>
      </template>
    </template>

    <!-- Schema Editor -->
    <template v-else>
      <div class="mb-4">
        <button
          type="button"
          class="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          data-testid="paramschema-back"
          @click="closeEditor"
        >
          <ChevronLeft class="h-4 w-4" aria-hidden="true" />
          {{ $t('views.ParameterSchemasView.back') }}
        </button>
      </div>

      <!-- Schema details tabs -->
      <Tabs v-model:value="activeEditorTab" class="w-full">
        <TabList>
          <Tab value="schema">{{ $t('views.ParameterSchemasView.tab_schema') }}</Tab>
          <Tab value="sets">{{ $t('views.ParameterSchemasView.tab_sets') }}</Tab>
          <Tab value="references">{{ $t('views.ParameterSchemasView.tab_references') }}</Tab>
          <Tab value="validate">{{ $t('views.ParameterSchemasView.tab_validate') }}</Tab>
        </TabList>

<TabPanels>

        <!-- Schema Tab -->
        <TabPanel value="schema" class="space-y-6 pt-4">
          <header class="flex items-center justify-between">
            <div>
              <h2 class="text-xl font-semibold">{{ isNew ? $t('views.ParameterSchemasView.new_schema_title') : schemaForm.name || $t('views.ParameterSchemasView.edit_schema_title') }}</h2>
              <p v-if="!isNew" class="text-sm text-muted-foreground">v{{ editingSchema?.version ?? 1 }}</p>
            </div>
            <div class="flex items-center gap-2">
              <Button :disabled="saving || !schemaForm.name.trim()" @click="saveSchema">
                {{ saving ? $t('views.ParameterSchemasView.saving') : (isNew ? $t('views.ParameterSchemasView.create') : $t('views.ParameterSchemasView.save_as_new_version')) }}
              </Button>
              <button
                type="button"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                @click="closeEditor"
              >
                {{ $t('views.ParameterSchemasView.cancel') }}
              </button>
            </div>
          </header>

          <div v-if="saveError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
            {{ saveError }}
          </div>
          <div v-if="saveSuccess" class="rounded-lg border border-success/50 bg-success/10 p-4 text-sm text-success">
            {{ saveSuccess }}
          </div>

          <div class="space-y-4">
            <div>
              <label for="paramschema-name" class="mb-1 block text-sm font-medium">{{ $t('views.ParameterSchemasView.name') }}</label>
              <input id="paramschema-name"
                v-model="schemaForm.name"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                :placeholder="$t('views.ParameterSchemasView.name_placeholder')"
                data-testid="paramschema-name-input"
              />
            </div>
            <div>
              <label for="paramschema-description" class="mb-1 block text-sm font-medium">{{ $t('views.ParameterSchemasView.description') }}</label>
              <textarea id="paramschema-description"
                v-model="schemaForm.description"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                rows="2"
                :placeholder="$t('views.ParameterSchemasView.desc_placeholder')"
                data-testid="paramschema-desc-input"
              />
            </div>
          </div>

          <!-- Parameter Editor -->
          <div>
            <div class="flex items-center justify-between mb-3">
              <h3 class="text-sm font-semibold">{{ $t('views.ParameterSchemasView.parameters') }}</h3>
              <Button severity="secondary" outlined size="small" @click="addParameter">
                {{ $t('views.ParameterSchemasView.add_parameter') }}
              </Button>
            </div>

            <div v-if="schemaForm.parameters.length === 0" class="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
              {{ $t('views.ParameterSchemasView.no_parameters') }}
            </div>

            <div v-else class="space-y-3">
              <div
                v-for="(param, idx) in schemaForm.parameters"
                :key="idx"
                class="rounded-lg border bg-card p-4"
                :data-testid="`paramschema-param-${idx}`"
              >
                <div class="flex items-start justify-between gap-2">
                  <div class="flex-1 space-y-3">
                    <div class="grid grid-cols-2 gap-3">
                      <div>
                      <label :for="'paramschema-param-name-' + idx" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.ParameterSchemasView.param_name') }}</label>
                      <input
                        :id="'paramschema-param-name-' + idx"
                        v-model="param.name"
                        type="text"
                        class="w-full rounded-lg border border-input bg-background px-3 py-1.5 text-sm font-mono"
                        :placeholder="$t('views.ParameterSchemasView.param_name_placeholder')"
                      />
                      </div>
                      <div>
                      <label :for="'paramschema-param-label-' + idx" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.ParameterSchemasView.param_label') }}</label>
                      <input
                        :id="'paramschema-param-label-' + idx"
                        v-model="param.label"
                        type="text"
                        class="w-full rounded-lg border border-input bg-background px-3 py-1.5 text-sm"
                        :placeholder="$t('views.ParameterSchemasView.param_label_placeholder')"
                      />
                      </div>
                    </div>
                    <div>
                      <label :for="'paramschema-param-desc-' + idx" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.ParameterSchemasView.param_desc') }}</label>
                      <input
                        :id="'paramschema-param-desc-' + idx"
                        v-model="param.description"
                        type="text"
                        class="w-full rounded-lg border border-input bg-background px-3 py-1.5 text-sm"
                        :placeholder="$t('views.ParameterSchemasView.param_desc_placeholder')"
                      />
                    </div>
                    <div class="grid grid-cols-2 gap-3">
                      <div>
                      <label :for="'paramschema-param-type-' + idx" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.ParameterSchemasView.param_type') }}</label>
                      <select
                        :id="'paramschema-param-type-' + idx"
                        v-model="param.type"
                        class="w-full rounded-lg border border-input bg-background px-3 py-1.5 text-sm"
                        @change="onParamTypeChange(param)"
                      >
                          <option value="string">string</option>
                          <option value="number">number</option>
                          <option value="boolean">boolean</option>
                          <option value="select">select</option>
                          <option value="model_backend_ref">model_backend_ref</option>
                          <option value="schema_ref">schema_ref</option>
                        </select>
                      </div>
                      <div class="flex items-end gap-2 pb-1">
                        <label class="flex items-center gap-1.5 text-sm cursor-pointer">
                          <input type="checkbox" v-model="param.required" class="h-4 w-4 rounded border-gray-300" />
                          {{ $t('views.ParameterSchemasView.param_required') }}
                        </label>
                        <label v-if="param.type === 'string'" class="flex items-center gap-1.5 text-sm cursor-pointer">
                          <input type="checkbox" v-model="param.multiline" class="h-4 w-4 rounded border-gray-300" />
                          {{ $t('views.ParameterSchemasView.param_multiline') }}
                        </label>
                      </div>
                    </div>
                    <div v-if="param.type === 'number'" class="grid grid-cols-2 gap-3">
                      <div>
                      <label :for="'paramschema-param-min-' + idx" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.ParameterSchemasView.param_min') }}</label>
                      <input
                        :id="'paramschema-param-min-' + idx"
                        v-model.number="param.minimum"
                        type="number"
                        class="w-full rounded-lg border border-input bg-background px-3 py-1.5 text-sm"
                      />
                      </div>
                      <div>
                      <label :for="'paramschema-param-max-' + idx" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.ParameterSchemasView.param_max') }}</label>
                      <input
                        :id="'paramschema-param-max-' + idx"
                        v-model.number="param.maximum"
                        type="number"
                        class="w-full rounded-lg border border-input bg-background px-3 py-1.5 text-sm"
                      />
                      </div>
                    </div>
                    <div v-if="param.type === 'select'">
                      <label :for="'paramschema-param-options-' + idx" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.ParameterSchemasView.param_options') }}</label>
                      <div class="space-y-1">
                        <div v-for="(_, oi) in (param.options || [])" :key="oi" class="flex items-center gap-1">
                          <input
                            v-model="(param.options ?? [])[oi]"
                            type="text"
                            :aria-label="$t('views.ParameterSchemasView.param_options')"
                            class="flex-1 rounded-lg border border-input bg-background px-3 py-1.5 text-sm"
                            :placeholder="$t('views.ParameterSchemasView.option_placeholder')"
                          />
                          <button
                            type="button"
                            class="rounded p-1 text-muted-foreground hover:text-destructive"
                            :aria-label="$t('views.ParameterSchemasView.remove_option')"
                            data-testid="paramschema-remove-option"
                            @click="(param.options ?? []).splice(oi, 1)"
                          >
                            <X class="h-3 w-3" aria-hidden="true" />
                          </button>
                        </div>
                        <button
                          type="button"
                          class="text-xs text-indigo-500 hover:text-indigo-400"
                          @click="(param.options = param.options || []).push('')"
                        >
                          {{ $t('views.ParameterSchemasView.add_option') }}
                        </button>
                      </div>
                    </div>
                    <div>
                      <label :for="'paramschema-param-default-' + idx" class="mb-1 block text-xs text-muted-foreground">{{ $t('views.ParameterSchemasView.param_default') }}</label>
                      <input
                        :id="'paramschema-param-default-' + idx"
                        v-if="param.type === 'string'"
                        v-model="param.default_value"
                        type="text"
                        class="w-full rounded-lg border border-input bg-background px-3 py-1.5 text-sm"
                      />
                      <input
                        :id="'paramschema-param-default-' + idx"
                        v-else-if="param.type === 'number'"
                        v-model.number="param.default_value"
                        type="number"
                        class="w-full rounded-lg border border-input bg-background px-3 py-1.5 text-sm"
                      />
                      <select
                        :id="'paramschema-param-default-' + idx"
                        v-else-if="param.type === 'boolean'"
                        v-model="param.default_value"
                        class="w-full rounded-lg border border-input bg-background px-3 py-1.5 text-sm"
                      >
                        <option :value="undefined"></option>
                        <option :value="true">true</option>
                        <option :value="false">false</option>
                      </select>
                      <select
                        :id="'paramschema-param-default-' + idx"
                        v-else-if="param.type === 'select' && param.options?.length"
                        v-model="param.default_value"
                        class="w-full rounded-lg border border-input bg-background px-3 py-1.5 text-sm"
                      >
                        <option :value="undefined"></option>
                        <option v-for="o in param.options" :key="o" :value="o">{{ o }}</option>
                      </select>
                      <span v-else class="text-sm text-muted-foreground">—</span>
                    </div>
                  </div>
                  <button
                    type="button"
                    class="shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-destructive"
                    :aria-label="$t('views.ParameterSchemasView.remove_param')"
                    @click="removeParameter(idx)"
                    data-testid="paramschema-remove-param"
                  >
                    <X class="h-4 w-4" aria-hidden="true" />
                  </button>
                </div>
              </div>
            </div>
          </div>
        </TabPanel>

        <!-- Parameter Sets Tab -->
        <TabPanel value="sets" class="space-y-4 pt-4">
          <header class="flex items-center justify-between">
            <h3 class="text-base font-semibold">{{ $t('views.ParameterSchemasView.parameter_sets') }}</h3>
            <Button size="small" @click="startNewSet" data-testid="paramschema-new-set">
              {{ $t('views.ParameterSchemasView.new_set') }}
            </Button>
          </header>

          <LoadingSpinner v-if="setsLoading" />
          <ErrorAlert v-else-if="setsError" :message="setsError" :on-retry="loadSets" />

          <div v-else-if="sets.length === 0" class="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
            {{ $t('views.ParameterSchemasView.no_sets') }}
          </div>

          <div v-else class="space-y-2">
            <div
              v-for="set in sets"
              :key="set.id"
              class="flex items-center justify-between rounded-lg border bg-card px-4 py-3"
            >
              <div>
                <p class="text-sm font-medium">{{ set.name }}</p>
                <p v-if="set.description" class="text-xs text-muted-foreground">{{ set.description }}</p>
              </div>
              <div class="flex items-center gap-1">
                <button
                  type="button"
                  class="rounded px-2 py-1 text-xs text-muted-foreground hover:bg-accent"
                  @click="editSet(set)"
                  data-testid="paramschema-edit-set"
                >
                  {{ $t('views.ParameterSchemasView.edit') }}
                </button>
                <button
                  type="button"
                  class="rounded px-2 py-1 text-xs text-muted-foreground hover:text-destructive"
                  @click="cloneSet(set)"
                  data-testid="paramschema-clone-set"
                >
                  {{ $t('views.ParameterSchemasView.clone') }}
                </button>
                <button
                  type="button"
                  class="rounded px-2 py-1 text-xs text-muted-foreground hover:text-destructive"
                  :aria-label="$t('views.ParameterSchemasView.delete')"
                  data-testid="paramschema-delete-set"
                  @click="confirmDeleteSet(set)"
                >
                  <Trash2 class="h-3 w-3" aria-hidden="true" />
                </button>
              </div>
            </div>
          </div>

          <!-- Set Editor -->
          <div v-if="editingSet" class="rounded-lg border bg-card p-4 space-y-4">
            <h4 class="text-sm font-semibold">{{ editingSetId ? $t('views.ParameterSchemasView.edit_set') : $t('views.ParameterSchemasView.new_set') }}</h4>

            <div class="space-y-3">
              <div>
              <label for="paramschema-set-name" class="mb-1 block text-sm font-medium">{{ $t('views.ParameterSchemasView.name') }}</label>
              <input id="paramschema-set-name"
                v-model="setForm.name"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                :placeholder="$t('views.ParameterSchemasView.set_name_placeholder')"
                data-testid="paramschema-set-name"
              />
              </div>
              <div>
              <label for="paramschema-set-desc" class="mb-1 block text-sm font-medium">{{ $t('views.ParameterSchemasView.description') }}</label>
              <input id="paramschema-set-desc"
                v-model="setForm.description"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                :placeholder="$t('views.ParameterSchemasView.desc_placeholder')"
              />
              </div>
            </div>

            <div class="space-y-3">
              <h5 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">{{ $t('views.ParameterSchemasView.parameter_values') }}</h5>
              <div
                v-for="param in schemaForm.parameters"
                :key="param.name"
                class="space-y-1"
              >
              <label :for="'paramschema-set-value-' + param.name" class="block text-xs font-medium text-muted-foreground">
                {{ param.label || param.name }}
                <span v-if="param.required" class="text-destructive">*</span>
              </label>

              <textarea
                v-if="param.type === 'string' && param.multiline"
                :id="'paramschema-set-value-' + param.name"
                v-model="setForm.values[param.name]"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                rows="3"
                :placeholder="param.placeholder || ''"
              />
              <input
                v-else-if="param.type === 'string'"
                :id="'paramschema-set-value-' + param.name"
                v-model="setForm.values[param.name]"
                type="text"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :placeholder="param.placeholder || ''"
                />
                <input
                  v-else-if="param.type === 'number'"
                  :id="'paramschema-set-value-' + param.name"
                  v-model.number="setForm.values[param.name]"
                  type="number"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  :min="param.minimum"
                  :max="param.maximum"
                />
                <select
                  v-else-if="param.type === 'boolean'"
                  :id="'paramschema-set-value-' + param.name"
                  v-model="setForm.values[param.name]"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                >
                  <option :value="undefined"></option>
                  <option :value="true">true</option>
                  <option :value="false">false</option>
                </select>
                <select
                  v-else-if="param.type === 'select'"
                  :id="'paramschema-set-value-' + param.name"
                  v-model="setForm.values[param.name]"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                >
                  <option :value="undefined"></option>
                  <option v-for="o in (param.options || [])" :key="o" :value="o">{{ o }}</option>
                </select>
                <div v-else-if="param.type === 'model_backend_ref'">
                  <select
                    :id="'paramschema-set-value-' + param.name"
                    v-model="setForm.values[param.name]"
                    class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  >
                    <option :value="undefined"></option>
                    <option v-for="mb in modelBackends" :key="mb.id" :value="mb.id">{{ mb.name || shortId(mb.id) }}</option>
                  </select>
                </div>
                <div v-else-if="param.type === 'schema_ref'">
                  <select
                    :id="'paramschema-set-value-' + param.name"
                    v-model="setForm.values[param.name]"
                    class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                  >
                    <option :value="undefined"></option>
                    <option v-for="s in allSchemas" :key="s.id" :value="s.id">{{ s.name || shortId(s.id) }}</option>
                  </select>
                </div>
              </div>
            </div>

            <div v-if="setSaveError" class="text-sm text-destructive">{{ setSaveError }}</div>
            <div class="flex items-center gap-2">
              <Button :disabled="setSaving || !setForm.name.trim()" @click="saveSet" data-testid="paramschema-set-save">
                {{ setSaving ? $t('views.ParameterSchemasView.saving') : $t('views.ParameterSchemasView.save') }}
              </Button>
              <button
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                @click="cancelSetEdit"
              >
                {{ $t('views.ParameterSchemasView.cancel') }}
              </button>
            </div>
          </div>

          <div v-if="deleteSetConfirmId" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4">
            <p class="text-sm font-medium text-destructive">
              {{ $t('views.ParameterSchemasView.delete_set_confirm', { name: deleteSetConfirmName }) }}
            </p>
            <div class="mt-3 flex items-center gap-2">
              <Button :disabled="deletingSet" severity="danger" @click="doDeleteSet">
                {{ deletingSet ? $t('views.ParameterSchemasView.deleting') : $t('views.ParameterSchemasView.delete') }}
              </Button>
              <button
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                @click="deleteSetConfirmId = null"
              >
                {{ $t('views.ParameterSchemasView.cancel') }}
              </button>
            </div>
          </div>
        </TabPanel>

        <!-- References Tab -->
        <TabPanel value="references" class="space-y-4 pt-4">
          <LoadingSpinner v-if="refsLoading" />
          <ErrorAlert v-else-if="refsError" :message="refsError" :on-retry="loadReferences" />

          <template v-else>
            <div v-if="!references" class="text-sm text-muted-foreground">
              {{ $t('views.ParameterSchemasView.select_schema_for_refs') }}
            </div>

            <div v-else>
              <h4 class="mb-2 text-sm font-semibold">{{ $t('views.ParameterSchemasView.agents_using') }} ({{ references.agents?.length || 0 }})</h4>
              <div v-if="!references.agents?.length" class="text-sm text-muted-foreground">—</div>
              <ul v-else class="space-y-1">
                <li v-for="agent in references.agents" :key="agent.id" class="text-sm">
                  <router-link :to="`/admin/agents/${agent.id}`" class="text-indigo-500 hover:text-indigo-400">
                    {{ agent.name || shortId(agent.id) }}
                  </router-link>
                </li>
              </ul>

              <h4 class="mt-4 mb-2 text-sm font-semibold">{{ $t('views.ParameterSchemasView.sets_using') }} ({{ references.sets?.length || 0 }})</h4>
              <div v-if="!references.sets?.length" class="text-sm text-muted-foreground">—</div>
              <ul v-else class="space-y-1">
                <li v-for="set in references.sets" :key="set.id" class="text-sm text-muted-foreground">
                  {{ set.name || shortId(set.id) }}
                </li>
              </ul>
            </div>
          </template>
        </TabPanel>

        <!-- Validate Tab -->
        <TabPanel value="validate" class="space-y-4 pt-4">
          <p class="text-sm text-muted-foreground">{{ $t('views.ParameterSchemasView.validate_desc') }}</p>

          <div class="space-y-2">
            <div
              v-for="param in schemaForm.parameters"
              :key="param.name"
              class="space-y-1"
            >
              <label :for="'paramschema-validate-value-' + param.name" class="block text-xs font-medium text-muted-foreground">{{ param.label || param.name }}</label>

              <textarea
                v-if="param.type === 'string' && param.multiline"
                :id="'paramschema-validate-value-' + param.name"
                v-model="validateValues[param.name]"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                rows="2"
              />
              <input
                v-else-if="param.type === 'string'"
                :id="'paramschema-validate-value-' + param.name"
                v-model="validateValues[param.name]"
                type="text"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              />
              <input
                v-else-if="param.type === 'number'"
                :id="'paramschema-validate-value-' + param.name"
                v-model.number="validateValues[param.name]"
                type="number"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              />
              <select
                v-else-if="param.type === 'boolean'"
                :id="'paramschema-validate-value-' + param.name"
                v-model="validateValues[param.name]"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              >
                <option :value="undefined"></option>
                <option :value="true">true</option>
                <option :value="false">false</option>
              </select>
              <select
                v-else-if="param.type === 'select'"
                :id="'paramschema-validate-value-' + param.name"
                v-model="validateValues[param.name]"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              >
                <option :value="undefined"></option>
                <option v-for="o in (param.options || [])" :key="o" :value="o">{{ o }}</option>
              </select>
            </div>
          </div>

          <div class="flex items-center gap-2">
            <Button :disabled="validating" @click="doValidate">
              {{ validating ? $t('views.ParameterSchemasView.validating') : $t('views.ParameterSchemasView.validate') }}
            </Button>
          </div>

          <div v-if="validateResult !== null" class="space-y-2">
            <div v-if="validateResult.valid" class="rounded-lg border border-success/50 bg-success/10 p-3 text-sm text-success">
              {{ $t('views.ParameterSchemasView.validation_passed') }}
            </div>
            <div v-else class="rounded-lg border border-destructive/50 bg-destructive/10 p-3">
              <p class="text-sm font-medium text-destructive">{{ $t('views.ParameterSchemasView.validation_failed') }}</p>
              <ul v-if="validateResult.errors?.length" class="mt-1 list-inside list-disc text-sm text-destructive/90">
                <li v-for="e in validateResult.errors" :key="e">{{ e.field || e.message || e }}</li>
              </ul>
            </div>
          </div>
        </TabPanel>
      </TabPanels>
</Tabs>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import { shortId } from '../utils/format'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import Button from 'primevue/button'
import Tabs from 'primevue/tabs'
import TabList from 'primevue/tablist'
import Tab from 'primevue/tab'
import TabPanels from 'primevue/tabpanels'
import TabPanel from 'primevue/tabpanel'
import { Trash2, ChevronLeft, X } from '@lucide/vue'

interface ParameterDef {
  name: string
  label?: string
  description?: string
  type: string
  required: boolean
  default_value?: any
  multiline: boolean
  options?: string[]
  minimum?: number
  maximum?: number
  placeholder?: string
}

interface SchemaItem {
  id: string
  organisation_id: string
  name: string
  description: string | null
  version: number
  parameters: ParameterDef[]
  created_at: string
  updated_at: string
  account_id?: string
}

interface SchemaListResponse {
  items: SchemaItem[]
  total: number
  page: number
  page_size: number
}

interface SetItem {
  id: string
  parameter_schema_id: string
  name: string
  description?: string
  version: number
  schema_version: number
  values: Record<string, any>
  created_at: string
  updated_at: string
}

interface ReferenceResponse {
  agents: Array<{ id: string; name?: string }>
  sets: Array<{ id: string; name?: string }>
}

interface ModelBackendItem {
  id: string
  name: string
}

const { t } = useI18n()

const { loading, error, data: schemasResp, load: loadSchemas } = useDataFetch<SchemaListResponse>(
  () => api.GET('/api/v1/parameter-schemas', {
    params: { query: { page: 1, page_size: 100 } },
  }).then(r => ({ data: r.data as unknown as SchemaListResponse, error: r.error })),
  { initialValue: { items: [], total: 0, page: 1, page_size: 100 } },
)

const schemas = computed(() => schemasResp.value?.items ?? [])

const editingSchema = ref<SchemaItem | null>(null)
const isNew = computed(() => !editingSchema.value?.id)
const saving = ref(false)
const saveError = ref<string | null>(null)
const saveSuccess = ref<string | null>(null)
const activeEditorTab = ref('schema')

const schemaForm = ref<{ name: string; description?: string; parameters: ParameterDef[] }>({
  name: '',
  description: '',
  parameters: [],
})

function createEmptyParam(): ParameterDef {
  return { name: '', label: '', description: '', type: 'string', required: false, default_value: undefined, multiline: false, options: [], minimum: undefined, maximum: undefined }
}

function onParamTypeChange(param: ParameterDef) {
  if (param.type !== 'select') param.options = undefined
  if (param.type !== 'number') { param.minimum = undefined; param.maximum = undefined }
  if (param.type !== 'string') param.multiline = false
}

function addParameter() {
  schemaForm.value.parameters.push(createEmptyParam())
}

function removeParameter(idx: number) {
  schemaForm.value.parameters.splice(idx, 1)
}

function startNewSchema() {
  editingSchema.value = null
  schemaForm.value = { name: '', description: '', parameters: [] }
  saveError.value = null
  saveSuccess.value = null
  activeEditorTab.value = 'schema'
}

function editSchema(schema: SchemaItem) {
  editingSchema.value = schema
  schemaForm.value = {
    name: schema.name,
    description: schema.description || '',
    parameters: (schema.parameters || []).map((p: any) => ({
      ...p,
      required: p.required ?? false,
      multiline: p.multiline ?? false,
    })),
  }
  saveError.value = null
  saveSuccess.value = null
  activeEditorTab.value = 'schema'
}

function closeEditor() {
  editingSchema.value = null
  saveError.value = null
  saveSuccess.value = null
  cancelSetEdit()
}

async function saveSchema() {
  saving.value = true
  saveError.value = null
  saveSuccess.value = null

  try {
    const payload = {
      name: schemaForm.value.name,
      description: schemaForm.value.description || null,
      parameters: schemaForm.value.parameters.map((p) => ({
        ...p,
        description: p.description || null,
        label: p.label || null,
        default_value: p.default_value ?? null,
        placeholder: p.placeholder || null,
        options: p.options?.length ? p.options : null,
      })),
    }

    let resp
    if (isNew.value) {
      resp = await api.POST('/api/v1/parameter-schemas', { body: payload as any })
    } else if (editingSchema.value) {
      resp = await api.PUT('/api/v1/parameter-schemas/{schema_id}', {
        params: { path: { schema_id: editingSchema.value.id } },
        body: { ...payload as any, version: editingSchema.value.version },
      })
    }

    if (resp?.error) {
      saveError.value = formatApiError(resp.error)
      return
    }
    saveSuccess.value = isNew.value
      ? t('views.ParameterSchemasView.schema_created')
      : t('views.ParameterSchemasView.schema_saved_new_version')
    await loadSchemas()
    if (!isNew.value && resp?.data) {
      editingSchema.value = resp.data as unknown as SchemaItem
    }
  } catch (err: any) {
    saveError.value = formatApiError(err)
  } finally {
    saving.value = false
  }
}

// Delete schema
const deleteConfirmId = ref<string | null>(null)
const deleteConfirmName = ref('')
const deleting = ref(false)
const deleteRefError = ref<string | null>(null)

function confirmDelete(schema: SchemaItem) {
  deleteConfirmId.value = schema.id
  deleteConfirmName.value = schema.name
  deleteRefError.value = null
}

async function doDelete() {
  if (!deleteConfirmId.value) return
  deleting.value = true
  deleteRefError.value = null
  try {
    const resp = await api.DELETE('/api/v1/parameter-schemas/{schema_id}', {
      params: { path: { schema_id: deleteConfirmId.value } },
    })
    if (resp.error) {
      const msg = formatApiError(resp.error)
      if (resp.response?.status === 409) {
        deleteRefError.value = t('views.ParameterSchemasView.delete_referenced_error')
      } else {
        deleteRefError.value = msg
      }
      return
    }
    deleteConfirmId.value = null
    await loadSchemas()
  } catch (err: any) {
    deleteRefError.value = formatApiError(err)
  } finally {
    deleting.value = false
  }
}

// Parameter Sets
const setsLoading = ref(false)
const setsError = ref<string | null>(null)
const sets = ref<SetItem[]>([])

const editingSet = ref(false)
const editingSetId = ref<string | null>(null)
const setForm = ref<{ name: string; description?: string; values: Record<string, any> }>({ name: '', description: '', values: {} })
const setSaving = ref(false)
const setSaveError = ref<string | null>(null)

async function loadSets() {
  if (!editingSchema.value?.id) return
  setsLoading.value = true
  setsError.value = null
  try {
    const resp = await api.GET('/api/v1/parameter-schemas/{schema_id}/sets', {
      params: { path: { schema_id: editingSchema.value.id } },
    })
    if (resp.error) {
      setsError.value = formatApiError(resp.error)
      return
    }
    sets.value = (resp.data as any) ?? []
  } catch (err: any) {
    setsError.value = formatApiError(err)
  } finally {
    setsLoading.value = false
  }
}

function startNewSet() {
  editingSet.value = true
  editingSetId.value = null
  setForm.value = { name: '', description: '', values: {} }
  setSaveError.value = null
}

function editSet(set: SetItem) {
  editingSet.value = true
  editingSetId.value = set.id
  setForm.value = { name: set.name, description: set.description || '', values: { ...(set.values || {}) } }
  setSaveError.value = null
}

function cloneSet(set: SetItem) {
  editingSet.value = true
  editingSetId.value = null
  setForm.value = { name: t('views.ParameterSchemasView.cloned_set_name', { name: set.name }), description: set.description || '', values: { ...(set.values || {}) } }
  setSaveError.value = null
}

function cancelSetEdit() {
  editingSet.value = false
  editingSetId.value = null
  setSaveError.value = null
}

async function saveSet() {
  if (!editingSchema.value?.id) return
  setSaving.value = true
  setSaveError.value = null
  try {
    const payload = { name: setForm.value.name, description: setForm.value.description || null, values: setForm.value.values }
    let resp
    if (editingSetId.value) {
      resp = await api.PUT('/api/v1/parameter-schemas/{schema_id}/sets/{set_id}', {
        params: { path: { schema_id: editingSchema.value.id, set_id: editingSetId.value } },
        body: { ...payload as any, version: sets.value.find(s => s.id === editingSetId.value)?.version ?? 1 },
      })
    } else {
      resp = await api.POST('/api/v1/parameter-schemas/{schema_id}/sets', {
        params: { path: { schema_id: editingSchema.value.id } },
        body: payload as any,
      })
    }
    if (resp?.error) {
      setSaveError.value = formatApiError(resp.error)
      return
    }
    cancelSetEdit()
    await loadSets()
  } catch (err: any) {
    setSaveError.value = formatApiError(err)
  } finally {
    setSaving.value = false
  }
}

// Delete set
const deleteSetConfirmId = ref<string | null>(null)
const deleteSetConfirmName = ref('')
const deletingSet = ref(false)

function confirmDeleteSet(set: SetItem) {
  deleteSetConfirmId.value = set.id
  deleteSetConfirmName.value = set.name
}

async function doDeleteSet() {
  if (!deleteSetConfirmId.value || !editingSchema.value?.id) return
  deletingSet.value = true
  try {
    const resp = await api.DELETE('/api/v1/parameter-schemas/{schema_id}/sets/{set_id}', {
      params: { path: { schema_id: editingSchema.value.id, set_id: deleteSetConfirmId.value } },
    })
    if (resp.error) {
      console.warn('Failed to delete set:', resp.error)
      return
    }
    deleteSetConfirmId.value = null
    await loadSets()
  } catch (err: any) {
    console.warn('Failed to delete set:', err)
  } finally {
    deletingSet.value = false
  }
}

// References
const refsLoading = ref(false)
const refsError = ref<string | null>(null)
const references = ref<ReferenceResponse | null>(null)

async function loadReferences() {
  if (!editingSchema.value?.id) return
  refsLoading.value = true
  refsError.value = null
  try {
    const resp = await api.GET('/api/v1/parameter-schemas/{schema_id}/references', {
      params: { path: { schema_id: editingSchema.value.id } },
    })
    if (resp.error) {
      refsError.value = formatApiError(resp.error)
      return
    }
    references.value = resp.data as unknown as ReferenceResponse
  } catch (err: any) {
    refsError.value = formatApiError(err)
  } finally {
    refsLoading.value = false
  }
}

watch(activeEditorTab, (tab) => {
  if (tab === 'sets') loadSets()
  if (tab === 'references') loadReferences()
})

// Validate
const validateValues = ref<Record<string, any>>({})
const validating = ref(false)
const validateResult = ref<{ valid: boolean; errors?: any[] } | null>(null)

async function doValidate() {
  if (!editingSchema.value?.id) return
  validating.value = true
  validateResult.value = null
  try {
    const resp = await api.POST('/api/v1/parameter-schemas/{schema_id}/validate', {
      params: { path: { schema_id: editingSchema.value.id } },
      body: { values: validateValues.value },
    })
    if (resp.error) {
      const errDetail = (resp.error as any)?.detail
      if (Array.isArray(errDetail)) {
        validateResult.value = { valid: false, errors: errDetail }
      } else {
        validateResult.value = { valid: false, errors: [{ message: formatApiError(resp.error) }] }
      }
      return
    }
    validateResult.value = { valid: true }
  } catch (err: any) {
    validateResult.value = { valid: false, errors: [{ message: formatApiError(err) }] }
  } finally {
    validating.value = false
  }
}

// Model backends and schemas for ref pickers
const modelBackends = ref<ModelBackendItem[]>([])
const allSchemas = ref<SchemaItem[]>([])

async function loadPickers() {
  try {
    const [mbResp, schResp] = await Promise.all([
      api.GET('/api/v1/model-backends', { params: { query: { page: 1, page_size: 100 } } }),
      api.GET('/api/v1/schemas', { params: { query: { page: 1, page_size: 100 } } }),
    ])
    if (mbResp.data) modelBackends.value = (mbResp.data as any)?.items ?? []
    if (schResp.data) allSchemas.value = (schResp.data as any)?.items ?? []
  } catch (e: unknown) {
    console.warn('Failed to load picker data', e)
  }
}
loadPickers()
</script>
