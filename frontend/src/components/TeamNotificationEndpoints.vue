<template>
  <div>
    <div v-if="loading" class="flex items-center justify-center py-4">
      <div
        class="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent"
      />
    </div>

    <div v-else-if="error" class="mb-3 text-sm text-destructive">
      {{ error }}
      <button
        class="ml-2 underline"
        data-testid="team-notif-retry"
        @click="loadEndpoints"
      >
        Retry
      </button>
    </div>

    <template v-else>
      <div
        v-if="teamEndpoints.length === 0 && !showAddForm"
        class="py-4 text-center text-sm text-muted-foreground"
      >
        No webhook endpoints configured for this team.
      </div>

      <div
        v-for="ep in teamEndpoints"
        :key="ep.id"
        class="mb-2 rounded-lg border p-3"
      >
        <div class="flex items-start justify-between">
          <div class="min-w-0 flex-1">
            <Tooltip :delay-duration="300">
              <TooltipTrigger as-child>
                <p
                  class="truncate font-mono text-sm"
                  :data-testid="'team-notif-url-' + ep.id"
                >
                  {{ ep.url }}
                </p>
              </TooltipTrigger>
              <TooltipContent side="top" class="max-w-xs">
                <p class="break-all">{{ ep.url }}</p>
              </TooltipContent>
            </Tooltip>
            <p
              v-if="ep.description"
              class="mt-0.5 text-xs text-muted-foreground"
            >
              {{ ep.description }}
            </p>
            <div class="mt-1 flex flex-wrap gap-1">
              <span
                v-for="evt in ep.events"
                :key="evt"
                class="inline-flex items-center rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
              >
                {{ evt }}
              </span>
            </div>
          </div>
          <div class="ml-2 flex shrink-0 items-center gap-1">
            <span
              class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium"
              :class="
                ep.auto_disabled
                  ? 'bg-destructive/10 text-destructive'
                  : 'bg-success/10 text-success'
              "
            >
              <span
                class="h-1.5 w-1.5 rounded-full"
                :class="ep.auto_disabled ? 'bg-destructive' : 'bg-success'"
              />
              {{ ep.auto_disabled ? "Disabled" : "Active" }}
            </span>
            <button
              class="rounded p-1 text-muted-foreground hover:bg-accent"
              data-testid="team-notif-edit"
              title="Edit"
              @click="startEdit(ep)"
            >
              <svg
                class="h-4 w-4"
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              >
                <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
              </svg>
            </button>
            <button
              class="rounded p-1 text-muted-foreground hover:text-destructive"
              data-testid="team-notif-test"
              title="Test"
              @click="test(ep)"
            >
              <svg
                class="h-4 w-4"
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              >
                <path d="M12 2a10 10 0 1 0 10 10h-10Z" />
              </svg>
            </button>
            <button
              class="rounded p-1 text-destructive hover:bg-destructive/10"
              data-testid="team-notif-delete"
              title="Delete"
              @click="confirmDelete(ep)"
            >
              <svg
                class="h-4 w-4"
                xmlns="http://www.w3.org/2000/svg"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
              >
                <path d="M3 6h18" />
                <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" />
                <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
              </svg>
            </button>
          </div>
        </div>

        <div
          v-if="testResults[ep.id]"
          class="mt-2 rounded bg-muted p-2 text-xs"
          :class="
            testResults[ep.id].success ? 'text-success' : 'text-destructive'
          "
        >
          <template v-if="testResults[ep.id].success">
            ✓ Test sent successfully (HTTP {{ testResults[ep.id].status_code }})
          </template>
          <template v-else>
            ✗ Test failed:
            {{
              testResults[ep.id].error ||
              "HTTP " + testResults[ep.id].status_code
            }}
          </template>
        </div>

        <div
          v-if="deleteConfirmId === ep.id"
          class="mt-3 rounded-lg border border-destructive/50 bg-destructive/10 p-3"
        >
          <p class="text-sm font-medium text-destructive">
            Delete this webhook endpoint?
          </p>
          <p class="mt-1 text-sm text-destructive/80">
            This will stop all notifications to this URL.
          </p>
          <div class="mt-3 flex items-center gap-2">
            <button
              :disabled="deleting"
              data-testid="team-notif-delete-confirm"
              class="rounded-lg bg-destructive px-3 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
              @click="deleteEndpoint(ep.id)"
            >
              {{ deleting ? "Deleting..." : "Delete" }}
            </button>
            <button
              class="rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium hover:bg-accent"
              @click="deleteConfirmId = null"
            >
              Cancel
            </button>
          </div>
        </div>

        <div
          v-if="editingId === ep.id"
          class="mt-3 space-y-3 rounded-lg border bg-muted/30 p-3"
        >
          <h4 class="text-sm font-medium">Edit Webhook</h4>
          <div>
            <label class="mb-1 block text-xs font-medium">URL</label>
            <input
              v-model="editForm.url"
              type="url"
              data-testid="team-notif-edit-url"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="https://example.com/webhook"
            />
          </div>
          <div>
            <label class="mb-1 block text-xs font-medium"
              >Secret
              <span class="text-muted-foreground"
                >(leave blank to keep existing)</span
              ></label
            >
            <input
              v-model="editForm.secret"
              type="password"
              data-testid="team-notif-edit-secret"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label class="mb-1 block text-xs font-medium">Events</label>
            <div class="flex flex-wrap gap-3">
              <label
                v-for="evt in availableEvents"
                :key="evt"
                class="flex items-center gap-1.5 text-sm"
              >
                <input
                  type="checkbox"
                  :value="evt"
                  v-model="editForm.events"
                  class="rounded border-input"
                />
                {{ evt }}
              </label>
            </div>
          </div>
          <div>
            <label class="mb-1 block text-xs font-medium">Description</label>
            <input
              v-model="editForm.description"
              type="text"
              data-testid="team-notif-edit-description"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            />
          </div>
          <div v-if="editError" class="text-sm text-destructive">
            {{ editError }}
          </div>
          <div class="flex items-center gap-2">
            <button
              :disabled="!editForm.url.trim() || saving"
              class="rounded-lg bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              data-testid="team-notif-edit-save"
              @click="saveEdit"
            >
              {{ saving ? "Saving..." : "Save" }}
            </button>
            <button
              class="rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium hover:bg-accent"
              data-testid="team-notif-edit-cancel"
              @click="cancelEdit"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>

      <div
        v-if="showAddForm && !editingId"
        class="mt-3 space-y-3 rounded-lg border bg-muted/30 p-3"
      >
        <h4 class="text-sm font-medium">New Webhook</h4>
        <div>
          <label class="mb-1 block text-xs font-medium">URL</label>
          <input
            v-model="addForm.url"
            type="url"
            data-testid="team-notif-add-url"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            placeholder="https://example.com/webhook"
          />
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium"
            >Secret <span class="text-muted-foreground">(optional)</span></label
          >
          <input
            v-model="addForm.secret"
            type="password"
            data-testid="team-notif-add-secret"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          />
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium">Events</label>
          <div class="flex flex-wrap gap-3">
            <label
              v-for="evt in availableEvents"
              :key="evt"
              class="flex items-center gap-1.5 text-sm"
            >
              <input
                type="checkbox"
                :value="evt"
                v-model="addForm.events"
                class="rounded border-input"
              />
              {{ evt }}
            </label>
          </div>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium">Description</label>
          <input
            v-model="addForm.description"
            type="text"
            data-testid="team-notif-add-description"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          />
        </div>
        <div v-if="addError" class="text-sm text-destructive">
          {{ addError }}
        </div>
        <div class="flex items-center gap-2">
          <button
            :disabled="!addForm.url.trim() || adding"
            class="rounded-lg bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            data-testid="team-notif-add-save"
            @click="addEndpoint"
          >
            {{ adding ? "Adding..." : "Add" }}
          </button>
          <button
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium hover:bg-accent"
            data-testid="team-notif-add-cancel"
            @click="cancelAdd"
          >
            Cancel
          </button>
        </div>
      </div>

      <button
        v-if="!showAddForm && !editingId"
        class="mt-3 flex items-center gap-1 text-sm text-primary hover:underline"
        data-testid="team-notif-add-button"
        @click="showAddForm = true"
      >
        <svg
          class="h-4 w-4"
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <path d="M5 12h14" />
          <path d="M12 5v14" />
        </svg>
        Add webhook
      </button>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { api } from "../lib/api/client";
import { formatApiError } from "../lib/api/formatError";
import type { components } from "../lib/api/client";
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from "../components/ui/tooltip";

type NotificationEndpointResponse =
  components["schemas"]["NotificationEndpointResponse"];
type TestResult = components["schemas"]["TestResult"];

const props = defineProps<{ teamId: string }>();

const availableEvents = [
  "hitl_awaiting",
  "run_failed",
  "claim_expired",
  "hitl_overdue",
];

const endpoints = ref<NotificationEndpointResponse[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);

const showAddForm = ref(false);
const addForm = ref({
  url: "",
  secret: "",
  events: [] as string[],
  description: "",
});
const adding = ref(false);
const addError = ref<string | null>(null);

const editingId = ref<string | null>(null);
const editForm = ref({
  url: "",
  secret: "",
  events: [] as string[],
  description: "",
});
const saving = ref(false);
const editError = ref<string | null>(null);

const deleteConfirmId = ref<string | null>(null);
const deleting = ref(false);

const testResults = ref<Record<string, TestResult>>({});
const testingId = ref<string | null>(null);

const teamEndpoints = computed(() =>
  endpoints.value.filter((ep) => ep.team_id === props.teamId),
);

async function loadEndpoints() {
  loading.value = true;
  error.value = null;
  try {
    const { data, error: err } = await api.GET("/api/v1/notifications");
    if (err) {
      error.value = `Failed to load endpoints: ${formatApiError(err)}`;
    } else if (data) {
      endpoints.value = data;
    }
  } catch (e: unknown) {
    error.value = `Failed to load endpoints: ${e instanceof Error ? e.message : String(e)}`;
  } finally {
    loading.value = false;
  }
}

function startEdit(ep: NotificationEndpointResponse) {
  cancelAdd();
  deleteConfirmId.value = null;
  editingId.value = ep.id;
  editForm.value = {
    url: ep.url,
    secret: "",
    events: [...ep.events],
    description: ep.description ?? "",
  };
  editError.value = null;
}

function cancelEdit() {
  editingId.value = null;
  editForm.value = { url: "", secret: "", events: [], description: "" };
  editError.value = null;
}

async function saveEdit() {
  if (!editingId.value || !editForm.value.url.trim()) return;
  saving.value = true;
  editError.value = null;
  try {
    const body: Record<string, unknown> = {
      url: editForm.value.url.trim(),
    };
    if (editForm.value.events.length > 0) body.events = editForm.value.events;
    else body.events = [];
    if (editForm.value.description)
      body.description = editForm.value.description;
    else body.description = null;
    if (editForm.value.secret) body.secret = editForm.value.secret;

    const { error: err } = await api.PUT(
      "/api/v1/notifications/{endpoint_id}",
      {
        params: { path: { endpoint_id: editingId.value } },
        body: body as any,
      },
    );
    if (err) {
      editError.value = `Save failed: ${formatApiError(err)}`;
    } else {
      cancelEdit();
      await loadEndpoints();
    }
  } catch (e: unknown) {
    editError.value = `Save failed: ${e instanceof Error ? e.message : String(e)}`;
  } finally {
    saving.value = false;
  }
}

function cancelAdd() {
  showAddForm.value = false;
  addForm.value = { url: "", secret: "", events: [], description: "" };
  addError.value = null;
}

async function addEndpoint() {
  if (!addForm.value.url.trim()) return;
  adding.value = true;
  addError.value = null;
  try {
    const body: Record<string, unknown> = {
      url: addForm.value.url.trim(),
      team_id: props.teamId,
    };
    if (addForm.value.secret) body.secret = addForm.value.secret;
    if (addForm.value.events.length > 0) body.events = addForm.value.events;
    if (addForm.value.description) body.description = addForm.value.description;

    const { data, error: err } = await api.POST("/api/v1/notifications", {
      body: body as any,
    });
    if (err) {
      addError.value = `Create failed: ${formatApiError(err)}`;
    } else if (data) {
      cancelAdd();
      await loadEndpoints();
    }
  } catch (e: unknown) {
    addError.value = `Create failed: ${e instanceof Error ? e.message : String(e)}`;
  } finally {
    adding.value = false;
  }
}

function confirmDelete(ep: NotificationEndpointResponse) {
  cancelEdit();
  cancelAdd();
  deleteConfirmId.value = ep.id;
}

async function deleteEndpoint(id: string) {
  deleting.value = true;
  try {
    const { error: err, response } = await api.DELETE(
      "/api/v1/notifications/{endpoint_id}",
      {
        params: { path: { endpoint_id: id } },
      },
    );
    if (err) {
      error.value = `Delete failed: ${formatApiError(err)}`;
    } else if (response.status === 204 || response.ok) {
      deleteConfirmId.value = null;
      await loadEndpoints();
    }
  } catch (e: unknown) {
    error.value = `Delete failed: ${e instanceof Error ? e.message : String(e)}`;
  } finally {
    deleting.value = false;
  }
}

async function test(ep: NotificationEndpointResponse) {
  if (testingId.value) return;
  testingId.value = ep.id;
  testResults.value[ep.id] = undefined as any;
  try {
    const { data, error: err } = await api.POST(
      "/api/v1/admin/notifications/{webhook_id}/test",
      {
        params: { path: { webhook_id: ep.id } },
      },
    );
    if (err) {
      testResults.value[ep.id] = {
        success: false,
        status_code: null,
        response_body: null,
        error: String(err),
      };
    } else if (data) {
      testResults.value[ep.id] = data;
    }
  } catch (e: unknown) {
    testResults.value[ep.id] = {
      success: false,
      status_code: null,
      response_body: null,
      error: e instanceof Error ? e.message : String(e),
    };
  } finally {
    testingId.value = null;
  }
}

onMounted(() => loadEndpoints());
</script>
