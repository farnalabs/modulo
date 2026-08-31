<template>
  <div class="space-y-5">
    <!-- Operational config (first-class flat fields) -->
    <section>
      <h3 class="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        {{ $t('connectors.rest.section_operational') }}
      </h3>
      <div class="space-y-4">
        <div>
          <label for="restconn-connector-base-url" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.base_url') }}</label>
          <input id="restconn-connector-base-url"
            v-model="config.base_url"
            type="url"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            :placeholder="$t('connectors.rest.base_url_placeholder')"
            data-testid="rest-connector-base-url"
            :aria-invalid="!!errors.base_url"
            :aria-describedby="errors.base_url ? 'restconn-base-url-error' : undefined"
          />
          <p v-if="errors.base_url" id="restconn-base-url-error" class="mt-1 text-sm text-destructive">{{ errors.base_url }}</p>
        </div>

        <div>
          <label for="restconn-connector-method" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.method') }}</label>
          <select id="restconn-connector-method"
            v-model="config.method"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            data-testid="rest-connector-method"
            :aria-invalid="!!errors.method"
            :aria-describedby="errors.method ? 'restconn-method-error' : undefined"
          >
            <option v-for="m in METHOD_OPTIONS" :key="m" :value="m">{{ m }}</option>
          </select>
          <p v-if="errors.method" id="restconn-method-error" class="mt-1 text-sm text-destructive">{{ errors.method }}</p>
        </div>

        <div>
          <label for="restconn-connector-timeout" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.timeout_seconds') }}</label>
          <input id="restconn-connector-timeout"
            v-model="config.timeout_seconds"
            type="number"
            min="1"
            step="1"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            data-testid="rest-connector-timeout"
            :aria-invalid="!!errors.timeout_seconds"
            :aria-describedby="errors.timeout_seconds ? 'restconn-timeout-error' : undefined"
          />
          <p v-if="errors.timeout_seconds" id="restconn-timeout-error" class="mt-1 text-sm text-destructive">{{ errors.timeout_seconds }}</p>
        </div>

        <div>
          <label class="flex cursor-pointer items-center gap-2 text-sm">
            <input v-model="config.verify_tls" type="checkbox" class="h-4 w-4 rounded border-input" data-testid="rest-connector-verify-tls" />
            <span class="font-medium">{{ $t('connectors.rest.verify_tls') }}</span>
            <span class="text-muted-foreground">{{ $t('connectors.rest.verify_tls_help') }}</span>
          </label>
        </div>

        <div>
          <label for="restconn-connector-on-unknown" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.on_unknown') }}</label>
          <select id="restconn-connector-on-unknown"
            v-model="config.on_unknown"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            data-testid="rest-connector-on-unknown"
            :aria-invalid="!!errors.on_unknown"
            :aria-describedby="errors.on_unknown ? 'restconn-on-unknown-error restconn-on-unknown-help' : 'restconn-on-unknown-help'"
          >
            <option v-for="o in ON_UNKNOWN_OPTIONS" :key="o" :value="o">{{ $t(`connectors.rest.on_unknown_${o}`) }}</option>
          </select>
          <p v-if="errors.on_unknown" id="restconn-on-unknown-error" class="mt-1 text-sm text-destructive">{{ errors.on_unknown }}</p>
          <p id="restconn-on-unknown-help" class="mt-1 text-xs text-muted-foreground">{{ $t(`connectors.rest.on_unknown_${config.on_unknown}_help`) }}</p>
        </div>

        <div>
          <label for="restconn-connector-records-path" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.records_path') }}</label>
          <input id="restconn-connector-records-path"
            v-model="config.records_path"
            type="text"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
            placeholder="data.items"
            data-testid="rest-connector-records-path"
            :aria-describedby="'restconn-records-path-help'"
          />
          <p id="restconn-records-path-help" class="mt-1 text-xs text-muted-foreground">{{ $t('connectors.rest.records_path_help') }}</p>
        </div>

        <div>
          <label for="restconn-connector-allowed-hosts" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.allowed_hosts') }}</label>
          <input id="restconn-connector-allowed-hosts"
            v-model="config.allowed_hosts"
            type="text"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            placeholder="api.example.com,cdn.example.com"
            data-testid="rest-connector-allowed-hosts"
            :aria-describedby="'restconn-allowed-hosts-help'"
          />
          <p id="restconn-allowed-hosts-help" class="mt-1 text-xs text-muted-foreground">{{ $t('connectors.rest.allowed_hosts_help') }}</p>
        </div>
      </div>
    </section>

    <!-- Credentials (auth) — never conflated with operational config -->
    <section>
      <h3 class="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        {{ $t('connectors.rest.section_credentials') }}
      </h3>
      <p v-if="mode === 'edit'" class="mb-3 text-xs text-muted-foreground">{{ $t('connectors.rest.credentials_write_only') }}</p>
      <div class="space-y-4">
        <div>
          <label for="restconn-connector-auth-mode" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.auth_mode') }}</label>
          <select id="restconn-connector-auth-mode"
            v-model="credentials.auth_mode"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            data-testid="rest-connector-auth-mode"
            :aria-invalid="!!errors.auth_mode"
            :aria-describedby="errors.auth_mode ? 'restconn-auth-mode-error' : undefined"
          >
            <option v-for="m in AUTH_MODE_OPTIONS" :key="m" :value="m">{{ $t(`connectors.rest.auth_mode_${m}`) }}</option>
          </select>
          <p v-if="errors.auth_mode" id="restconn-auth-mode-error" class="mt-1 text-sm text-destructive">{{ errors.auth_mode }}</p>
        </div>

        <template v-if="credentials.auth_mode === 'bearer'">
          <div>
            <label for="restconn-connector-token" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.token') }}</label>
            <input id="restconn-connector-token"
              v-model="credentials.token"
              type="password"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="rest-connector-token"
              :aria-invalid="!!errors.token"
              :aria-describedby="errors.token ? 'restconn-token-error' : undefined"
            />
            <p v-if="errors.token" id="restconn-token-error" class="mt-1 text-sm text-destructive">{{ errors.token }}</p>
          </div>
        </template>

        <template v-else-if="credentials.auth_mode === 'basic'">
          <div>
            <label for="restconn-connector-username" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.username') }}</label>
            <input id="restconn-connector-username"
              v-model="credentials.username"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="rest-connector-username"
              :aria-invalid="!!errors.username"
              :aria-describedby="errors.username ? 'restconn-username-error' : undefined"
            />
            <p v-if="errors.username" id="restconn-username-error" class="mt-1 text-sm text-destructive">{{ errors.username }}</p>
          </div>
          <div>
            <label for="restconn-connector-password" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.password') }}</label>
            <input id="restconn-connector-password"
              v-model="credentials.password"
              type="password"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="rest-connector-password"
              :aria-invalid="!!errors.password"
              :aria-describedby="errors.password ? 'restconn-password-error' : undefined"
            />
            <p v-if="errors.password" id="restconn-password-error" class="mt-1 text-sm text-destructive">{{ errors.password }}</p>
          </div>
        </template>

        <template v-else-if="credentials.auth_mode === 'api_key'">
          <div>
            <label for="restconn-connector-api-key" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.api_key') }}</label>
            <input id="restconn-connector-api-key"
              v-model="credentials.api_key"
              type="password"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="rest-connector-api-key"
              :aria-invalid="!!errors.api_key"
              :aria-describedby="errors.api_key ? 'restconn-api-key-error' : undefined"
            />
            <p v-if="errors.api_key" id="restconn-api-key-error" class="mt-1 text-sm text-destructive">{{ errors.api_key }}</p>
          </div>
          <div>
            <label for="restconn-connector-api-key-in" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.api_key_in') }}</label>
            <select id="restconn-connector-api-key-in"
              v-model="credentials.apiKeyIn"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="rest-connector-api-key-in"
            >
              <option value="header">{{ $t('connectors.rest.auth_in_header') }}</option>
              <option value="query">{{ $t('connectors.rest.auth_in_query') }}</option>
            </select>
          </div>
          <div v-if="credentials.apiKeyIn === 'header'">
            <label for="restconn-connector-header-name" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.header_name') }}</label>
            <input id="restconn-connector-header-name"
              v-model="credentials.header_name"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="rest-connector-header-name"
            />
          </div>
          <div v-else>
            <label for="restconn-connector-query-param" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.query_param_name') }}</label>
            <input id="restconn-connector-query-param"
              v-model="credentials.query_param_name"
              type="text"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="rest-connector-query-param"
            />
          </div>
        </template>
      </div>
    </section>

    <!-- Advanced (templated) fields — JSON editor -->
    <section>
      <h3 class="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        {{ $t('connectors.rest.section_advanced') }}
      </h3>
      <div>
        <label for="restconn-connector-advanced" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.advanced_json') }}</label>
        <textarea id="restconn-connector-advanced"
          v-model="config.advanced_json"
          rows="8"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
          placeholder='{ "path": "/items", "headers": { "Accept": "application/json" }, "operations": {}, "fan_out": {} }'
          data-testid="rest-connector-advanced-json"
          :aria-invalid="!!errors.advanced_json"
          :aria-describedby="errors.advanced_json ? 'restconn-advanced-error' : 'restconn-advanced-help'"
        ></textarea>
        <p id="restconn-advanced-help" class="mt-1 text-xs text-muted-foreground">{{ $t('connectors.rest.advanced_json_help') }}</p>
        <p v-if="errors.advanced_json" id="restconn-advanced-error" class="mt-1 text-sm text-destructive">{{ errors.advanced_json }}</p>
      </div>
    </section>
  </div>
</template>

<script lang="ts">
// The canonical field surfaces for the Generic REST connector (FAR-466). These
// are the single source of truth the AdminConnectorsView prefill and the
// config_schema parity guard both rely on, so a drift in the connector schema
// is caught rather than silently losing config on an edit round-trip.
export const REST_FLAT_FIELDS = [
  'base_url',
  'method',
  'timeout_seconds',
  'verify_tls',
  'on_unknown',
  'records_path',
  'allowed_hosts',
] as const

// Advanced / templated fields stay a JSON editor. Mirror the connector's
// config_schema.advanced_fields exactly. `body_template` is a phantom key (the
// connector reads `body`); `rate_limit` IS read by the connector and is
// surfaced here so it is not silently dropped.
export const REST_ADVANCED_FIELDS = [
  'path',
  'headers',
  'params',
  'body',
  'operations',
  'next_cursor_path',
  'passthrough',
  'max_response_size',
  'idempotency_header',
  'fan_out',
  'rate_limit',
] as const
</script>

<script setup lang="ts">
import { reactive, ref, watch, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'

export interface RestConfigState {
  base_url: string
  method: string
  timeout_seconds: number | string
  verify_tls: boolean
  on_unknown: string
  records_path: string
  allowed_hosts: string
  advanced_json: string
}

export interface RestCredsState {
  auth_mode: string
  token: string
  username: string
  password: string
  api_key: string
  apiKeyIn: string
  header_name: string
  query_param_name: string
}

const config = defineModel<RestConfigState>('config', { required: true })
const credentials = defineModel<RestCredsState>('credentials', { required: true })
const credsDirty = defineModel<boolean>('credsDirty', { default: false })
// Separate SECRET (secret) dirtiness from AUTH-IDENTITY dirtiness so an
// identity-only edit (auth_mode / apiKeyIn / header_name / query_param_name)
// re-sends the credentials payload (and the backend overlays the new identity
// while preserving the stored secret), without ever triggering the secret
// clobber invariant or demanding a re-entered secret (FAR-466).
const credsIdentityDirty = defineModel<boolean>('credsIdentityDirty', { default: false })

const props = defineProps<{ mode: 'add' | 'edit' | null }>()
const { t } = useI18n()

const METHOD_OPTIONS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']
const ON_UNKNOWN_OPTIONS = ['fail_open', 'fail_closed', 'off']
const AUTH_MODE_OPTIONS = ['bearer', 'basic', 'api_key']

const errors = reactive<Record<string, string>>({})

function validate(): boolean {
  Object.keys(errors).forEach(k => { errors[k] = '' })

  const baseUrl = String(config.value.base_url || '').trim()
  if (!baseUrl) {
    errors.base_url = t('connectors.rest.base_url_required')
  } else {
    try {
      const parsedBaseUrl = new URL(baseUrl)
      // The connector is HTTP-only — reject any non http/https scheme (e.g.
      // mailto:, ftp:, file:) that `new URL()` would otherwise accept.
      if (parsedBaseUrl.protocol !== 'http:' && parsedBaseUrl.protocol !== 'https:') {
        errors.base_url = t('connectors.rest.base_url_invalid')
      }
    } catch {
      errors.base_url = t('connectors.rest.base_url_invalid')
    }
  }
  const method = String(config.value.method || '').toUpperCase()
  if (method && !METHOD_OPTIONS.includes(method)) {
    errors.method = t('connectors.rest.method_invalid', { method, options: METHOD_OPTIONS.join(', ') })
  }
  const timeout = config.value.timeout_seconds
  const numTimeout = Number(timeout)
  if (timeout === '' || timeout === null || Number.isNaN(numTimeout) || !Number.isInteger(numTimeout) || numTimeout < 1) {
    errors.timeout_seconds = t('connectors.rest.timeout_invalid')
  }
  const onUnknown = config.value.on_unknown
  if (onUnknown && !ON_UNKNOWN_OPTIONS.includes(onUnknown)) {
    errors.on_unknown = t('connectors.rest.on_unknown_invalid', { value: onUnknown, options: ON_UNKNOWN_OPTIONS.join(', ') })
  }
  const authMode = credentials.value.auth_mode
  if (authMode && !AUTH_MODE_OPTIONS.includes(authMode)) {
    errors.auth_mode = t('connectors.rest.auth_mode_invalid', { value: authMode })
  }
  // CRITICAL (FAR-466): editing an existing connector must not demand a
  // credential the user has not re-entered. In edit mode, the stored secret is
  // write-only and read back as empty, so a prefill (credsDirty === false)
  // must not be forced to validate a secret. Only validate a credential the
  // user actually edited. On create it stays strictly required.
  //
  // Exception (FAR-466 iteration 5): a SWITCH of auth_mode is a secret-requiring
  // edit even when no secret was re-typed. auth_mode lives in IDENTITY_FIELDS,
  // so changing it flips credsIdentityDirty (NOT credsDirty); the old
  // `!credsDirty` gate therefore stayed TRUE and suppressed the secret checks
  // on a mode change — reproducing edit an api_key connector → switch to bearer
  // → save without a token → backend preserveStoredSecret keeps the old secret
  // under auth_mode=bearer → `_normalise_auth` raises
  // "REST bearer auth requires creds['token']" on every later query. So a mode
  // change away from the mount-time (stored) auth_mode must run the NEW mode's
  // required-field checks, while an identity-only edit that leaves auth_mode
  // untouched (e.g. header_name) must still save without demanding a secret.
  const modeChanged = baselineAuthMode.value !== null
    && credentials.value.auth_mode !== baselineAuthMode.value
  const editingExisting = props.mode === 'edit' && !credsDirty.value && !modeChanged
  if (!editingExisting) {
    if (authMode === 'bearer' && !credentials.value.token) {
      errors.token = t('connectors.rest.token_required')
    }
    if (authMode === 'basic') {
      if (!credentials.value.username) errors.username = t('connectors.rest.username_required')
      if (!credentials.value.password) errors.password = t('connectors.rest.password_required')
    }
    if (authMode === 'api_key' && !credentials.value.api_key) {
      errors.api_key = t('connectors.rest.api_key_required')
    }
  }
  if (config.value.advanced_json && config.value.advanced_json.trim()) {
    try {
      JSON.parse(config.value.advanced_json)
      errors.advanced_json = ''
    } catch {
      errors.advanced_json = t('connectors.rest.advanced_json_invalid')
    }
  }

  return !Object.values(errors).some(v => v)
}

// CRITICAL (FAR-466): dirtiness is tracked in two independent channels so the
// secret-preservation invariant holds by construction AND an identity-only edit
// actually persists.
//   - credsDirty reflects ONLY a genuine user edit of the SECRET credential
//     fields (token / api_key / username / password). The secret is write-only;
//     the presence of a real secret edit is what decides whether the credentials
//     payload is re-sent as a secret replacement, and it is what the validate()
//     gate uses to decide whether a re-entered secret is demanded.
//   - credsIdentityDirty reflects ANY edit of the NON-SECRET auth identity
//     (auth_mode / apiKeyIn / header_name / query_param_name). An identity-only
//     edit must re-send the credentials payload so the backend overlays the new
//     identity onto the stored (decrypted) credential while preserving the
//     secret. Crucially, an identity edit never flips credsDirty — so it never
//     demands a re-entered secret, and the secret-preservation invariant still
//     holds (the parent's PATCH gate is `credsDirty || credsIdentityDirty`,
//     and the backend only replaces a secret field when the request supplies a
//     real value).
// Both baselines are captured at mount and compared per-field-set, so a
// programmatic prefill/reset never marks either channel dirty.
//
// auth_mode is additionally captured on its own so validate() can distinguish a
// real MODE SWITCH from an identity-only edit. A switch away from the stored
// mode demands the NEW mode's secret; an identity-only edit must not (FAR-466).
const SECRET_FIELDS = ['token', 'api_key', 'username', 'password'] as const
const IDENTITY_FIELDS = ['auth_mode', 'apiKeyIn', 'header_name', 'query_param_name'] as const
function secretSnapshot(): string {
  return JSON.stringify(SECRET_FIELDS.map(f => credentials.value[f]))
}
function identitySnapshot(): string {
  return JSON.stringify(IDENTITY_FIELDS.map(f => credentials.value[f]))
}
const credsBaseline = ref(secretSnapshot())
const identityBaseline = ref(identitySnapshot())
// The auth_mode echoed by the edit prefill (via prefillRestConfig). Non-null
// only after mount; a null baseline means validate() ran before the prefill
// settled, in which case a mode switch cannot be detected and the conservative
// credsDirty-only gate is used.
const baselineAuthMode = ref<string | null>(null)
onMounted(() => {
  credsBaseline.value = secretSnapshot()
  identityBaseline.value = identitySnapshot()
  baselineAuthMode.value = credentials.value.auth_mode ?? null
})
watch(
  () => secretSnapshot(),
  (v) => { credsDirty.value = v !== credsBaseline.value },
)
watch(
  () => identitySnapshot(),
  (v) => { credsIdentityDirty.value = v !== identityBaseline.value },
)

defineExpose({ validate })
</script>
