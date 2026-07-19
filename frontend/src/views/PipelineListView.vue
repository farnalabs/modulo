<template>
  <div class="min-h-screen bg-background flex flex-col">
    <header class="bg-card border-b border-border px-6 py-4">
      <div class="mx-auto flex items-center justify-between gap-3 max-w-6xl">
        <PageHeader :title="$t('views.PipelineListView.title')" />
        <FilterBar
          :search="{ placeholder: $t('views.PipelineListView.search_pipelines') }"
          :search-value="search"
          @update:search="search = $event; page = 1"
        />
        <div class="flex items-center gap-1 border border-border rounded-lg p-0.5" role="group" :aria-label="$t('views.PipelineListView.view_mode')">
          <button
            :class="viewMode === 'table' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:text-foreground'"
            class="rounded p-1.5 transition-colors"
            @click="setViewMode('table')"
            :aria-label="$t('views.PipelineListView.table_view')"
            :aria-pressed="viewMode === 'table'"
            data-testid="pipeline-view-toggle-table"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="3" x2="21" y2="3"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="3" y1="21" x2="21" y2="21"/></svg>
          </button>
          <button
            :class="viewMode === 'card' ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:text-foreground'"
            class="rounded p-1.5 transition-colors"
            @click="setViewMode('card')"
            :aria-label="$t('views.PipelineListView.card_view')"
            :aria-pressed="viewMode === 'card'"
            data-testid="pipeline-view-toggle-card"
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
          </button>
        </div>
          <Button
            v-if="allPipelines.length > 0 && !loading"
            variant="default"
            as="router-link"
            to="/library"
            data-testid="pipeline-list-new-pipeline"
          >
            {{ $t('views.PipelineListView.new_pipeline') }}
          </Button>
      </div>
    </header>

    <div class="flex flex-1 min-h-0">
      <!-- Folder sidebar -->
      <FolderTree
        :selected-folder-id="selectedFolderId"
        @select-folder="onSelectFolder"
        @folders-changed="loadPipelines"
      />
      <p v-if="folderError" class="px-4 py-2 text-xs text-destructive">
        Failed to load folders: {{ folderError }}
      </p>

      <main role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()" class="flex-1 page-wide">
      <div v-if="loading" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div v-for="i in 6" :key="i" class="card p-5 animate-pulse">
          <div class="h-5 w-3/4 bg-muted rounded mb-2" />
          <div class="h-3 w-full bg-muted rounded mb-1" />
          <div class="h-3 w-2/3 bg-muted rounded mb-4" />
          <div class="h-4 w-16 bg-muted rounded mb-3" />
          <div class="h-9 w-full bg-muted rounded" />
        </div>
      </div>

      <ErrorAlert v-else-if="error" :message="error" :on-retry="loadPipelines" class="mb-6" />

      <div v-else-if="filteredPipelines.length === 0 && search" class="text-center py-16">
        <p class="text-lg font-medium text-foreground">{{ $t('views.PipelineListView.no_pipelines_match_your_search') }}</p>
        <p class="text-sm text-muted-foreground mt-1">{{ $t('views.PipelineListView.try_a_different_search_term') }}</p>
      </div>

      <div v-else-if="allPipelines.length === 0 && !search" class="text-center py-16">
        <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" class="mx-auto mb-4 text-muted-foreground/40"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
        <p class="text-lg font-medium text-foreground">{{ $t('views.PipelineListView.no_pipelines_yet') }}</p>
        <p class="text-sm text-muted-foreground mt-1 mb-6">
          Create a new pipeline or browse the Library to find a template.
        </p>
        <div class="flex items-center justify-center gap-3">
          <Button
            variant="default"
            as="router-link"
            to="/library"
            data-testid="pipeline-list-new-pipeline"
          >
            New Pipeline
          </Button>
          <Button
            variant="outline"
            as="router-link"
            to="/library"
            data-testid="pipeline-list-browse-library"
          >
            Browse Library
          </Button>
        </div>
      </div>

      <div v-else>
        <!-- Card view -->
        <div v-if="viewMode === 'card'" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
            v-for="p in pagedPipelines"
            :key="p.id"
            class="card card-hover p-5 cursor-pointer"
            @click="openPipeline(p)"
            data-testid="pipeline-list-card"
          >
            <div class="flex items-start justify-between gap-2 mb-3">
              <h3 class="text-base font-medium text-foreground truncate">{{ p.name }}</h3>
              <div class="flex items-center gap-1 shrink-0">
                <span
                  v-if="p.archived_at"
                  class="badge text-xs badge-status-warning"
                >{{ $t('views.PipelineListView.archived') }}</span>
                <span
                  class="badge text-xs"
                  :class="p.visibility === 'org' ? 'badge-context-blue' : 'badge-context-purple'"
                  data-testid="pipeline-list-visibility-badge"
                >
                  {{ p.visibility === 'org' ? 'Org' : 'Team' }}
                </span>
                <DropdownMenu>
                  <DropdownMenuTrigger as-child>
                    <button class="rounded p-1 hover:bg-accent" data-testid="pipeline-list-action-menu" @click.stop>
                      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/></svg>
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" class="w-40">
                    <DropdownMenuItem @click="openRename(p)">Rename</DropdownMenuItem>
                    <DropdownMenuItem v-if="!p.archived_at" @click="handleArchive(p)">Archive</DropdownMenuItem>
                    <DropdownMenuItem v-else @click="handleUnarchive(p)">Unarchive</DropdownMenuItem>
                    <DropdownMenuItem @click="openMoveToFolder(p)">{{ $t('views.PipelineListView.move_to_folder') }}</DropdownMenuItem>
                    <DropdownMenuItem v-if="planStore.featureEnabled('pipeline_delete')" @click="openDelete(p)" class="text-destructive focus:text-destructive">Delete</DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            </div>

            <p v-if="p.description" class="text-sm text-muted-foreground mb-4 line-clamp-2">
              {{ p.description }}
            </p>
            <div v-else class="mb-10" />

            <div class="flex items-center gap-3 text-xs text-muted-foreground">
              <span class="flex items-center gap-1">
                <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 8v4l3 3"/><circle cx="12" cy="12" r="10"/></svg>
                Created {{ formatDate(p.created_at) }}
              </span>
            </div>

            <div class="mt-4 pt-3 border-t border-border flex gap-2">
              <Button
                variant="default"
                class="flex-1"
                data-testid="pipeline-list-open-editor"
              >
                {{ $t('views.PipelineListView.open_in_editor') }}
              </Button>
              <Button
                variant="outline"
                class="flex-1"
                @click.stop="openRunDialog(p)"
                data-testid="pipeline-list-run"
              >
                {{ $t('views.PipelineListView.run') }}
              </Button>
            </div>
          </div>
        </div>

        <!-- Table / Tree view -->
        <div v-else class="card rounded-lg border border-border overflow-hidden">
          <table class="w-full text-left text-sm">
            <thead class="bg-muted/50 text-xs font-medium uppercase text-muted-foreground">
              <tr>
                <th class="px-4 py-3">Name</th>
                <th class="px-4 py-3">Description</th>
                <th class="px-4 py-3">Visibility</th>
                <th class="px-4 py-3">Created</th>
                <th class="px-4 py-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <template v-for="(row, i) in treeRows" :key="i">
                <tr v-if="row.type === 'folder'" class="bg-muted/20 hover:bg-muted/30 transition-colors" data-testid="pipeline-tree-folder-row">
                  <td colspan="5" class="px-4 py-2">
                    <button
                      class="flex w-full items-center gap-2 text-sm font-medium text-foreground text-left"
                      @click="toggleFolder((row.data as FolderItem).id)"
                      :aria-expanded="expandedFolders.has((row.data as FolderItem).id)"
                      data-testid="pipeline-tree-folder-toggle"
                    >
                      <svg
                        xmlns="http://www.w3.org/2000/svg"
                        width="14"
                        height="14"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="2"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                        :class="{ 'rotate-90': expandedFolders.has((row.data as FolderItem).id) }"
                        class="transition-transform shrink-0"
                      >
                        <polyline points="9 18 15 12 9 6" />
                      </svg>
                      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
                      {{ (row.data as FolderItem).name }}
                      <span class="text-muted-foreground text-xs ml-auto">{{ pipelineFolderCount.get((row.data as FolderItem).id) || 0 }} {{ $t('views.PipelineListView.pipelines') }}</span>
                    </button>
                  </td>
                </tr>

                <tr v-else-if="row.type === 'uncategorised-header'" class="bg-muted/20">
                  <td colspan="5" class="px-4 py-2">
                    <span class="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                      {{ $t('views.PipelineListView.uncategorised') }}
                    </span>
                  </td>
                </tr>

                <tr
                  v-else-if="row.type === 'pipeline'"
                  class="cursor-pointer transition-colors hover:bg-muted/30"
                  @click="openPipeline(row.data as PipelineItem)"
                  :data-testid="`pipeline-tree-row-${(row.data as PipelineItem).id}`"
                >
                  <td class="px-4 py-3" :style="{ paddingLeft: `${12 + (row.depth || 0) * 16}px` }">
                    <span class="font-medium text-foreground">{{ (row.data as PipelineItem).name }}</span>
                  </td>
                  <td class="px-4 py-3">
                    <span v-if="(row.data as PipelineItem).description" class="text-muted-foreground truncate block max-w-xs">{{ (row.data as PipelineItem).description }}</span>
                    <span v-else class="text-muted-foreground/50 italic">{{ $t('views.PipelineListView.no_description') }}</span>
                  </td>
                  <td class="px-4 py-3">
                    <span class="badge text-xs" :class="(row.data as PipelineItem).visibility === 'org' ? 'badge-context-blue' : 'badge-context-purple'">
                      {{ (row.data as PipelineItem).visibility === 'org' ? 'Org' : 'Team' }}
                    </span>
                  </td>
                  <td class="px-4 py-3">
                    <span class="text-muted-foreground">{{ formatDate((row.data as PipelineItem).created_at) }}</span>
                  </td>
                  <td class="px-4 py-3">
                    <div class="flex justify-end">
                      <DropdownMenu>
                        <DropdownMenuTrigger as-child>
                          <button class="rounded p-1 hover:bg-accent" data-testid="pipeline-list-action-menu" @click.stop>
                            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/></svg>
                          </button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" class="w-40">
                          <DropdownMenuItem @click.prevent.stop="openRename(row.data as PipelineItem)">Rename</DropdownMenuItem>
                          <DropdownMenuItem v-if="!(row.data as PipelineItem).archived_at" @click.prevent.stop="handleArchive(row.data as PipelineItem)">Archive</DropdownMenuItem>
                          <DropdownMenuItem v-else @click.prevent.stop="handleUnarchive(row.data as PipelineItem)">Unarchive</DropdownMenuItem>
                          <DropdownMenuItem @click.prevent.stop="openMoveToFolder(row.data as PipelineItem)">{{ $t('views.PipelineListView.move_to_folder') }}</DropdownMenuItem>
                          <DropdownMenuItem v-if="planStore.featureEnabled('pipeline_delete')" @click.prevent.stop="openDelete(row.data as PipelineItem)" class="text-destructive focus:text-destructive">Delete</DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="viewMode === 'card' && totalPages > 1 && !loading" class="flex justify-center items-center gap-2 mt-8">
        <button
          :disabled="page <= 1"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="prevPage"
          data-testid="pipeline-list-prev-page"
        >
          {{ $t('views.PipelineListView.previous') }}
        </button>
        <span class="px-4 py-2 text-sm text-muted-foreground">
          {{ $t('views.PipelineListView.page_x_of_y', { page, total: totalPages }) }}
        </span>
        <button
          :disabled="page >= totalPages"
          class="px-4 py-2 text-sm border border-input bg-background rounded-lg disabled:opacity-30 hover:bg-accent transition-colors"
          @click="nextPage"
          data-testid="pipeline-list-next-page"
        >
          {{ $t('views.PipelineListView.next') }}
        </button>
      </div>
    </main>
    </div>
      <!-- Run dialog modal -->
      <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
        v-if="showRunDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        @click.self="closeRunDialog"
      >
        <div class="bg-card border border-border rounded-xl shadow-xl w-full max-w-lg mx-4 p-6 space-y-4">
          <div class="flex items-center justify-between">
            <h2 class="text-base font-semibold text-foreground">{{ $t('views.PipelineListView.run_pipeline') }}</h2>
            <button
              class="text-muted-foreground hover:text-foreground transition-colors"
              @click="closeRunDialog"
              data-testid="pipeline-list-run-dialog-close"
              aria-label="Close"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>

          <p class="text-sm text-muted-foreground">
            Run <span class="font-medium text-foreground">{{ selectedPipeline?.name }}</span>
          </p>

          <div class="space-y-2">
            <label for="pipelinelistview-field-3" class="block text-sm font-medium text-foreground">{{ $t('views.PipelineListView.prompt') }}</label>
            <textarea id="pipelinelistview-field-3"
              v-model="prompt"
              placeholder="Enter a prompt (optional)"
              rows="4"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm resize-none focus:outline-none focus:ring-2 focus:ring-primary"
              data-testid="pipeline-list-run-prompt"
            />
          </div>

          <div>
            <button
              class="text-sm text-muted-foreground hover:text-foreground transition-colors flex items-center gap-1"
              @click="showAdvanced = !showAdvanced"
              data-testid="pipeline-list-run-advanced-toggle"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                :class="{ 'rotate-180': showAdvanced }"
                class="transition-transform"
              ><polyline points="6 9 12 15 18 9"/></svg>
              {{ $t('views.PipelineListView.advanced') }}
            </button>
          </div>

          <div v-if="showAdvanced" class="space-y-2">
            <label for="pipelinelistview-field-2" class="block text-sm font-medium text-foreground">Input Payload (JSON)</label>
            <textarea id="pipelinelistview-field-2"
              v-model="advancedPayload"
              placeholder='{"prompt": "...", "temperature": 0.7}'
              rows="4"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono resize-none focus:outline-none focus:ring-2 focus:ring-primary"
              data-testid="pipeline-list-run-advanced-payload"
            />
          </div>

          <div v-if="runError" class="rounded-lg bg-destructive/10 border border-destructive/30 p-3 text-sm text-destructive" data-testid="pipeline-list-run-error">
            {{ runError }}
          </div>

          <div class="flex justify-end gap-2 pt-2">
            <button
              class="px-4 py-2 border border-input bg-background text-foreground text-sm font-medium rounded-lg hover:bg-accent transition-colors"
              @click="closeRunDialog"
              data-testid="pipeline-list-run-cancel"
            >
              {{ $t('common.cancel') }}
            </button>
            <Button
              variant="default"
              :disabled="running"
              class="border-primary/30"
              @click="triggerRun"
              data-testid="pipeline-list-run-submit"
            >
              <svg
                v-if="running"
                class="animate-spin h-4 w-4"
                xmlns="http://www.w3.org/2000/svg"
                fill="none"
                viewBox="0 0 24 24"
              >
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              {{ running ? $t('views.PipelineListView.running') : $t('views.PipelineListView.run_pipeline') }}
            </Button>
          </div>
        </div>
      </div>

      <!-- Move to Folder dialog -->
      <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
        v-if="showMoveToFolder"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        @click.self="showMoveToFolder = false"
      >
        <div class="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
          <h3 class="mb-4 text-lg font-semibold">{{ $t('views.PipelineListView.move_to_folder') }}</h3>
          <div class="space-y-3">
            <button
              v-for="f in foldersList"
              :key="f.id"
              class="flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
              :class="moveToFolderId === f.id ? 'border-primary bg-accent' : 'border-border'"
              @click="moveToFolderId = f.id"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0 text-muted-foreground"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
              {{ f.name }}
            </button>
            <button
              class="flex w-full items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm hover:bg-accent transition-colors text-left"
              :class="moveToFolderId === null ? 'border-primary bg-accent' : ''"
              @click="moveToFolderId = null"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="shrink-0 text-muted-foreground"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
              {{ $t('views.PipelineListView.no_folder') }}
            </button>
          </div>
          <div v-if="moveError" class="mt-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ moveError }}
          </div>
          <div class="mt-4 flex justify-end gap-2">
            <button class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent" @click="showMoveToFolder = false">
              {{ $t('common.cancel') }}
            </button>
            <Button :disabled="moving" @click="handleMoveToFolder">
              {{ moving ? $t('common.saving') : $t('common.save') }}
            </Button>
          </div>
        </div>
      </div>

      <!-- Rename dialog -->
      <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
        v-if="showRenameDialog"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        @click.self="showRenameDialog = false"
      >
        <div class="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
          <h3 class="mb-4 text-lg font-semibold">Rename Pipeline</h3>
          <div class="space-y-4">
            <div>
              <label for="pipelinelistview-field-1" class="mb-1 block text-sm font-medium">Name</label>
              <input id="pipelinelistview-field-1"
                v-model="renameName"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                placeholder="Pipeline name"
                @keyup.enter="handleRename"
              />
            </div>
            <div v-if="renameError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
              {{ renameError }}
            </div>
            <div class="flex justify-end gap-2">
              <button
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
                @click="showRenameDialog = false"
              >
                Cancel
              </button>
              <Button
                :disabled="!renameName.trim() || renaming"
                @click="handleRename"
              >
                {{ renaming ? 'Saving...' : 'Save' }}
              </Button>
            </div>
          </div>
        </div>
      </div>

      <!-- Delete confirmation dialog -->
      <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
        v-if="showDeleteConfirm"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
        @click.self="showDeleteConfirm = false"
      >
        <div class="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
          <h3 class="mb-4 text-lg font-semibold text-destructive">Delete Pipeline</h3>
          <p class="mb-4 text-sm text-muted-foreground">
            Are you sure? This permanently deletes the pipeline and all its runs.
          </p>
          <div v-if="deleteError" class="mb-4 rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ deleteError }}
          </div>
          <div class="flex justify-end gap-2">
            <button
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showDeleteConfirm = false"
            >
              Cancel
            </button>
            <button
              class="rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90"
              @click="handleDelete"
            >
              Delete
            </button>
          </div>
        </div>
      </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import FolderTree from '../components/pipelines/FolderTree.vue'
import { useDataFetch } from '../composables/useDataFetch'
import { usePlanStore } from '../stores/planStore'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { formatApiError } from '../lib/api/formatError'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '@/components/ui/dropdown-menu'
import { api } from '../lib/api/client'
import { useApi } from '../composables/useApi'
import { formatDateShort } from '../lib/formatDate'


interface PipelineItem {
  id: string
  organisation_id: string
  name: string
  description: string | null
  visibility: string
  created_at: string
  updated_at: string
  archived_at: string | null
  folder_id?: string | null
}

interface FolderItem {
  id: string
  organisation_id: string
  name: string
  parent_id: string | null
  sort_order: number
}

interface PipelineListResponse {
  items: PipelineItem[]
  total: number
  page: number
  page_size: number
}

const router = useRouter()
const planStore = usePlanStore()
const { get, post: postUntyped, patch: patchUntyped } = useApi()

const selectedFolderId = ref<string | null>(null)

const { loading, error, data: pipelinesResp, load: loadPipelines } = useDataFetch<PipelineListResponse>(
  async () => {
    const params: Record<string, any> = { page_size: 100 }
    if (selectedFolderId.value) {
      params.folder_id = selectedFolderId.value
    }
    const response = await api.GET('/api/v1/pipelines', { params: { query: params } })
    return { data: response.data as unknown as PipelineListResponse | undefined, error: response.error }
  },
  { initialValue: { items: [] as PipelineItem[], total: 0, page: 1, page_size: 100 } },
)

const allPipelines = computed(() => pipelinesResp.value?.items ?? [])

const foldersList = ref<FolderItem[]>([])
const folderError = ref<string | null>(null)

async function loadFolders() {
  folderError.value = null
  try {
    foldersList.value = await get<FolderItem[]>('/api/v1/pipeline-folders')
  } catch (e) {
    folderError.value = formatApiError(e)
    console.warn('Failed to load folders', e)
  }
}

const folderNameMap = computed(() => {
  const map = new Map<string, string>()
  for (const f of foldersList.value) {
    map.set(f.id, f.name)
  }
  return map
})

function onSelectFolder(folderId: string | null) {
  selectedFolderId.value = folderId
  page.value = 1
  loadPipelines()
  loadFolders()
}

// Move to folder state
const showMoveToFolder = ref(false)
const moveTarget = ref<PipelineItem | null>(null)
const moveToFolderId = ref<string | null>(null)
const moving = ref(false)
const moveError = ref<string | null>(null)
const showRenameDialog = ref(false)
const renameTarget = ref<PipelineItem | null>(null)
const renameName = ref('')
const renameError = ref<string | null>(null)
const renaming = ref(false)
const showDeleteConfirm = ref(false)
const deleteTarget = ref<PipelineItem | null>(null)
const deleteError = ref<string | null>(null)
const showRunDialog = ref(false)
const selectedPipeline = ref<PipelineItem | null>(null)
const prompt = ref('')
const showAdvanced = ref(false)
const advancedPayload = ref('')
const running = ref(false)
const runError = ref<string | null>(null)
const search = ref('')
const page = ref(1)
const pageSize = 12

const viewMode = ref<'card' | 'table'>(
  (() => {
    const stored = localStorage.getItem('pipeline-view-mode')
    return stored === 'card' || stored === 'table' ? stored : 'table'
  })()
)

function setViewMode(mode: 'card' | 'table') {
  viewMode.value = mode
  localStorage.setItem('pipeline-view-mode', mode)
}

interface TreeRow {
  type: 'folder' | 'pipeline' | 'uncategorised-header'
  depth: number
  data: PipelineItem | FolderItem | null
}

const expandedFolders = ref<Set<string>>(new Set())

function toggleFolder(folderId: string) {
  const next = new Set(expandedFolders.value)
  if (next.has(folderId)) {
    next.delete(folderId)
  } else {
    next.add(folderId)
  }
  expandedFolders.value = next
}

const pipelineFolderCount = computed(() => {
  const count = new Map<string, number>()
  for (const p of filteredPipelines.value) {
    if (p.folder_id) {
      count.set(p.folder_id, (count.get(p.folder_id) || 0) + 1)
    }
  }
  return count
})

const treeRows = computed<TreeRow[]>(() => {
  const rows: TreeRow[] = []
  const sortedFolders = [...foldersList.value].sort((a, b) => a.name.localeCompare(b.name))

  for (const folder of sortedFolders) {
    const pipelineCount = pipelineFolderCount.value.get(folder.id) || 0
    if (pipelineCount === 0) continue

    rows.push({ type: 'folder', depth: 0, data: folder })

    if (expandedFolders.value.has(folder.id)) {
      const folderPipelines = filteredPipelines.value
        .filter(p => p.folder_id === folder.id)
        .sort((a, b) => a.name.localeCompare(b.name))
      for (const p of folderPipelines) {
        rows.push({ type: 'pipeline', depth: 1, data: p })
      }
    }
  }

  const uncategorised = filteredPipelines.value
    .filter(p => !p.folder_id || !folderNameMap.value.has(p.folder_id))
    .sort((a, b) => a.name.localeCompare(b.name))

  if (uncategorised.length > 0) {
    rows.push({ type: 'uncategorised-header', depth: 0, data: null })
    for (const p of uncategorised) {
      rows.push({ type: 'pipeline', depth: 1, data: p })
    }
  }

  return rows
})

const filteredPipelines = computed(() => {
  const q = search.value.toLowerCase().trim()
  if (!q) return allPipelines.value
  return allPipelines.value.filter(p =>
    p.name.toLowerCase().includes(q) ||
    (p.description?.toLowerCase() ?? '').includes(q)
  )
})

const totalPages = computed(() => Math.ceil(filteredPipelines.value.length / pageSize))

const pagedPipelines = computed(() => {
  const start = (page.value - 1) * pageSize
  return filteredPipelines.value.slice(start, start + pageSize)
})

function prevPage() {
  page.value--
}

function nextPage() {
  page.value++
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr
  return formatDateShort(d)
}

function openPipeline(p: PipelineItem) {
  router.push({ name: 'pipeline-editor', params: { id: p.id } })
}

function openRunDialog(p: PipelineItem) {
  selectedPipeline.value = p
  prompt.value = ''
  showAdvanced.value = false
  advancedPayload.value = ''
  runError.value = null
  showRunDialog.value = true
}

function openRename(p: PipelineItem) {
  renameTarget.value = p
  renameName.value = p.name
  renameError.value = null
  showRenameDialog.value = true
}

function openMoveToFolder(p: PipelineItem) {
  moveTarget.value = p
  moveToFolderId.value = p.folder_id ?? null
  moveError.value = null
  showMoveToFolder.value = true
}

async function handleMoveToFolder() {
  if (!moveTarget.value) return
  moving.value = true
  moveError.value = null
  try {
    const folderId = moveToFolderId.value ?? null
    await patchUntyped(`/api/v1/pipelines/${moveTarget.value.id}/folder`, {
      folder_id: folderId,
    })
    showMoveToFolder.value = false
    moveTarget.value = null
    await loadPipelines()
    await loadFolders()
  } catch (e: unknown) {
    moveError.value = formatApiError(e)
  } finally {
    moving.value = false
  }
}

async function handleRename() {
  if (!renameTarget.value || !renameName.value.trim()) return
  renaming.value = true
  renameError.value = null
  try {
    await api.PATCH('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: renameTarget.value.id } },
      body: { name: renameName.value.trim() },
    })
    showRenameDialog.value = false
    await loadPipelines()
  } catch (e: unknown) {
    renameError.value = formatApiError(e)
  } finally {
    renaming.value = false
  }
}

async function handleArchive(p: PipelineItem) {
  try {
    await postUntyped(`/api/v1/pipelines/${p.id}/archive`)
    await loadPipelines()
  } catch (e) {
    error.value = formatApiError(e)
  }
}

async function handleUnarchive(p: PipelineItem) {
  try {
    await postUntyped(`/api/v1/pipelines/${p.id}/unarchive`)
    await loadPipelines()
  } catch (e) {
    error.value = formatApiError(e)
  }
}

function openDelete(p: PipelineItem) {
  deleteTarget.value = p
  deleteError.value = null
  showDeleteConfirm.value = true
}

async function handleDelete() {
  if (!deleteTarget.value) return
  deleteError.value = null
  try {
    await api.DELETE('/api/v1/pipelines/{pipeline_id}', {
      params: { path: { pipeline_id: deleteTarget.value.id } },
    })
    showDeleteConfirm.value = false
    deleteTarget.value = null
    router.push('/pipelines')
    await loadPipelines()
  } catch (e: unknown) {
    deleteError.value = formatApiError(e)
  }
}

function closeRunDialog() {
  showRunDialog.value = false
  selectedPipeline.value = null
  prompt.value = ''
  runError.value = null
}

async function triggerRun() {
  if (!selectedPipeline.value) return
  running.value = true
  runError.value = null
  try {
    let inputPayload: Record<string, unknown>
    if (showAdvanced.value && advancedPayload.value.trim()) {
      try {
        inputPayload = JSON.parse(advancedPayload.value)
      } catch {
        runError.value = 'Invalid JSON in advanced payload'
        running.value = false
        return
      }
    } else if (prompt.value.trim()) {
      inputPayload = { prompt: prompt.value }
    } else {
      inputPayload = {}
    }
    const { data } = await api.POST('/api/v1/runs', {
      body: {
        pipeline_id: selectedPipeline.value.id,
        input_payload: inputPayload,
      },
    })
    showRunDialog.value = false
    if (data) router.push({ name: 'run-detail', params: { id: (data as any).id } })
  } catch (e) {
    runError.value = formatApiError(e)
  } finally {
    running.value = false
  }
}

onMounted(() => {
  loadFolders()
})
</script>
