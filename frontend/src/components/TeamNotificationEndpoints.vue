<template>
  <div>
    <div v-if="loading" class="flex items-center justify-center py-4">
      <div
        class="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent"
      />
    </div>

    <div v-else-if="error" class="mb-3 text-sm text-destructive">
      {{ error }}
      <button type="button"
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
            <p
              class="truncate font-mono text-sm"
              :data-testid="'team-notif-url-' + ep.id"
              v-tooltip.top="{ value: ep.url, showDelay: 300 }"
            >
              {{ ep.url }}
            </p>
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
          <div v-if="canManage" class="ml-2 flex shrink-0 items-center gap-1">
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
            <button type="button"
              class="rounded p-1 text-muted-foreground hover:bg-accent"
              data-testid="team-notif-edit"
              title="Edit"
              aria-label="Edit"
              @click="startEdit(ep)"
            >
              <Pencil class="h-4 w-4" />
            </button>
            <button type="button"
              class="rounded p-1 text-muted-foreground hover:text-destructive"
              data-testid="team-notif-test"
              title="Test"
              aria-label="Test"
              @click="test(ep)"
            >
              <Play class="h-4 w-4" />
            </button>
            <button type="button"
              class="rounded p-1 text-destructive hover:bg-destructive/10"
              data-testid="team-notif-delete"
              title="Delete"
              aria-label="Delete"
              @click="confirmDelete(ep)"
            >
              <Trash2 class="h-4 w-4" />
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
            ✓ Test sent successfully (HTTP {{ testResults[ep.id]['status_code'] }})
          </template>
          <template v-else>
            ✗ Test failed:
            {{
              testResults[ep.id].error ||
              "HTTP " + testResults[ep.id]['status_code']
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
            <button type="button"
              :disabled="deleting"
              data-testid="team-notif-delete-confirm"
              class="rounded-lg bg-destructive px-3 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
              @click="deleteEndpoint(ep.id)"
            >
              {{ deleting ? "Deleting..." : "Delete" }}
            </button>
            <button type="button"
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
          <h4 class="text-sm font-medium">{{ $t('components.TeamNotificationEndpoints.edit_webhook') }}</h4>
          <div>
            <label for="teamnotificationendpoints-field-6" class="mb-1 block text-xs font-medium">{{ $t('components.TeamNotificationEndpoints.url') }}</label>
            <input id="teamnotificationendpoints-field-6"
              v-model="editForm.url"
              type="url"
              data-testid="team-notif-edit-url"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="https://example.com/webhook"
            />
          </div>
          <div>
            <label for="teamnotificationendpoints-field-5" class="mb-1 block text-xs font-medium"
              >{{ $t('components.TeamNotificationEndpoints.secret') }}
              <span class="text-muted-foreground"
                >{{ $t('components.TeamNotificationEndpoints.leave_blank_to_keep_existing') }}</span
              ></label
            >
            <input id="teamnotificationendpoints-field-5"
              v-model="editForm.secret"
              type="password"
              data-testid="team-notif-edit-secret"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            />
          </div>
          <div>
            <span class="mb-1 block text-xs font-medium">{{ $t('components.TeamNotificationEndpoints.events') }}</span>
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
            <label for="teamnotificationendpoints-field-4" class="mb-1 block text-xs font-medium">{{ $t('components.TeamNotificationEndpoints.description') }}</label>
            <input id="teamnotificationendpoints-field-4"
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
            <Button :disabled="!editForm.url.trim() || saving" data-testid="team-notif-edit-save" @click="saveEdit">
              {{ saving ? "Saving..." : "Save" }}
            </Button>
            <button type="button"
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
        <h4 class="text-sm font-medium">{{ $t('components.TeamNotificationEndpoints.new_webhook') }}</h4>
        <div>
          <label for="teamnotificationendpoints-field-3" class="mb-1 block text-xs font-medium">{{ $t('components.TeamNotificationEndpoints.url') }}</label>
          <input id="teamnotificationendpoints-field-3"
            v-model="addForm.url"
            type="url"
            data-testid="team-notif-add-url"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            placeholder="https://example.com/webhook"
          />
        </div>
        <div>
          <label for="teamnotificationendpoints-field-2" class="mb-1 block text-xs font-medium"
            >{{ $t('components.TeamNotificationEndpoints.secret') }} <span class="text-muted-foreground">{{ $t('components.TeamNotificationEndpoints.optional') }}</span></label
          >
          <input id="teamnotificationendpoints-field-2"
            v-model="addForm.secret"
            type="password"
            data-testid="team-notif-add-secret"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
          />
        </div>
        <div>
          <span class="mb-1 block text-xs font-medium">{{ $t('components.TeamNotificationEndpoints.events') }}</span>
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
          <label for="teamnotificationendpoints-field-1" class="mb-1 block text-xs font-medium">{{ $t('components.TeamNotificationEndpoints.description') }}</label>
          <input id="teamnotificationendpoints-field-1"
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
          <Button :disabled="!addForm.url.trim() || adding" data-testid="team-notif-add-save" @click="addEndpoint">
            {{ adding ? "Adding..." : "Add" }}
          </Button>
          <button type="button"
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium hover:bg-accent"
            data-testid="team-notif-add-cancel"
            @click="cancelAdd"
          >
            Cancel
          </button>
        </div>
      </div>

      <button type="button"
        v-if="canManage && !showAddForm && !editingId"
        class="mt-3 flex items-center gap-1 text-sm text-primary hover:underline"
        data-testid="team-notif-add-button"
        @click="showAddForm = true"
      >
        <Plus class="h-4 w-4" />
        Add webhook
      </button>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { api, getAccessToken } from "../lib/api/client";
import { decodeJwtPayload } from "../lib/jwt";
import Button from 'primevue/button'
import { formatApiError } from "../lib/api/formatError";
import type { components } from "../lib/api/client";
import { Pencil, Play, Trash2, Plus } from "@lucide/vue";

type NotificationEndpointResponse =
  components["schemas"]["NotificationEndpointResponse"];
type NotificationEndpointCreate =
  components["schemas"]["NotificationEndpointCreate"];
type NotificationEndpointUpdate =
  components["schemas"]["NotificationEndpointUpdate"];
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

// notification.manage resolves to operator; only operator+ may create/update/
// delete webhook endpoints (SECURITY #1462).
const canManage = computed(() => {
  const payload = decodeJwtPayload(getAccessToken()) as Record<string, unknown> | null;
  const role = payload?.org_role as string | undefined;
  return role === "operator" || role === "admin";
});

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
    error.value = `Failed to load endpoints: ${formatApiError(e)}`;
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
    const body: NotificationEndpointUpdate = {
      url: editForm.value.url.trim(),
      events: editForm.value.events,
      description: editForm.value.description || null,
    };
    if (editForm.value.secret) body.secret = editForm.value.secret;

    const { error: err } = await api.PUT(
      "/api/v1/notifications/{endpoint_id}",
      {
        params: { path: { endpoint_id: editingId.value } },
        body,
      },
    );
    if (err) {
      editError.value = `Save failed: ${formatApiError(err)}`;
    } else {
      cancelEdit();
      await loadEndpoints();
    }
  } catch (e: unknown) {
    editError.value = `Save failed: ${formatApiError(e)}`;
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
    const body: NotificationEndpointCreate = {
      url: addForm.value.url.trim(),
      team_id: props.teamId,
    };
    if (addForm.value.secret) body.secret = addForm.value.secret;
    if (addForm.value.events.length > 0) body.events = addForm.value.events;
    if (addForm.value.description) body.description = addForm.value.description;

    const { data, error: err } = await api.POST("/api/v1/notifications", {
      body,
    });
    if (err) {
      addError.value = `Create failed: ${formatApiError(err)}`;
    } else if (data) {
      cancelAdd();
      await loadEndpoints();
    }
  } catch (e: unknown) {
    addError.value = `Create failed: ${formatApiError(e)}`;
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
    error.value = `Delete failed: ${formatApiError(e)}`;
  } finally {
    deleting.value = false;
  }
}

async function test(ep: NotificationEndpointResponse) {
  if (testingId.value) return;
  testingId.value = ep.id;
  delete testResults.value[ep.id];
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
      error: formatApiError(e),
    };
  } finally {
    testingId.value = null;
  }
}

onMounted(() => loadEndpoints());
</script>
