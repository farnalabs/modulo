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
            placeholder="https://api.example.com"
            data-testid="rest-connector-base-url"
          />
          <p v-if="errors.base_url" class="mt-1 text-sm text-destructive">{{ errors.base_url }}</p>
        </div>

        <div>
          <label for="restconn-connector-method" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.method') }}</label>
          <select id="restconn-connector-method"
            v-model="config.method"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            data-testid="rest-connector-method"
          >
            <option v-for="m in METHOD_OPTIONS" :key="m" :value="m">{{ m }}</option>
          </select>
          <p v-if="errors.method" class="mt-1 text-sm text-destructive">{{ errors.method }}</p>
        </div>

        <div>
          <label for="restconn-connector-timeout" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.timeout_seconds') }}</label>
          <input id="restconn-connector-timeout"
            v-model="config.timeout_seconds"
            type="number"
            min="1"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            data-testid="rest-connector-timeout"
          />
          <p v-if="errors.timeout_seconds" class="mt-1 text-sm text-destructive">{{ errors.timeout_seconds }}</p>
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
          >
            <option v-for="o in ON_UNKNOWN_OPTIONS" :key="o" :value="o">{{ $t(`connectors.rest.on_unknown_${o}`) }}</option>
          </select>
          <p v-if="errors.on_unknown" class="mt-1 text-sm text-destructive">{{ errors.on_unknown }}</p>
          <p class="mt-1 text-xs text-muted-foreground">{{ $t(`connectors.rest.on_unknown_${config.on_unknown}_help`) }}</p>
        </div>

        <div>
          <label for="restconn-connector-records-path" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.records_path') }}</label>
          <input id="restconn-connector-records-path"
            v-model="config.records_path"
            type="text"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm"
            placeholder="data.items"
            data-testid="rest-connector-records-path"
          />
          <p class="mt-1 text-xs text-muted-foreground">{{ $t('connectors.rest.records_path_help') }}</p>
        </div>

        <div>
          <label for="restconn-connector-allowed-hosts" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.allowed_hosts') }}</label>
          <input id="restconn-connector-allowed-hosts"
            v-model="config.allowed_hosts"
            type="text"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            placeholder="api.example.com,cdn.example.com"
            data-testid="rest-connector-allowed-hosts"
          />
          <p class="mt-1 text-xs text-muted-foreground">{{ $t('connectors.rest.allowed_hosts_help') }}</p>
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
          >
            <option v-for="m in AUTH_MODE_OPTIONS" :key="m" :value="m">{{ $t(`connectors.rest.auth_mode_${m}`) }}</option>
          </select>
          <p v-if="errors.auth_mode" class="mt-1 text-sm text-destructive">{{ errors.auth_mode }}</p>
        </div>

        <template v-if="credentials.auth_mode === 'bearer'">
          <div>
            <label for="restconn-connector-token" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.token') }}</label>
            <input id="restconn-connector-token"
              v-model="credentials.token"
              type="password"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="rest-connector-token"
            />
            <p v-if="errors.token" class="mt-1 text-sm text-destructive">{{ errors.token }}</p>
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
            />
            <p v-if="errors.username" class="mt-1 text-sm text-destructive">{{ errors.username }}</p>
          </div>
          <div>
            <label for="restconn-connector-password" class="mb-1 block text-sm font-medium">{{ $t('connectors.rest.password') }}</label>
            <input id="restconn-connector-password"
              v-model="credentials.password"
              type="password"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              data-testid="rest-connector-password"
            />
            <p v-if="errors.password" class="mt-1 text-sm text-destructive">{{ errors.password }}</p>
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
            />
            <p v-if="errors.api_key" class="mt-1 text-sm text-destructive">{{ errors.api_key }}</p>
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
        ></textarea>
        <p class="mt-1 text-xs text-muted-foreground">{{ $t('connectors.rest.advanced_json_help') }}</p>
        <p v-if="errors.advanced_json" class="mt-1 text-sm text-destructive">{{ errors.advanced_json }}</p>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { reactive, watch } from 'vue'

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

defineProps<{ mode: 'add' | 'edit' | null }>()

const METHOD_OPTIONS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']
const ON_UNKNOWN_OPTIONS = ['fail_open', 'fail_closed', 'off']
const AUTH_MODE_OPTIONS = ['bearer', 'basic', 'api_key']

const errors = reactive<Record<string, string>>({})

function validate(): boolean {
  Object.keys(errors).forEach(k => { errors[k] = '' })

  const method = String(config.value.method || '').toUpperCase()
  if (method && !METHOD_OPTIONS.includes(method)) {
    errors.method = `Invalid method ${method}; expected one of ${METHOD_OPTIONS.join(', ')}`
  }
  const timeout = config.value.timeout_seconds
  if (timeout === '' || timeout === null || Number.isNaN(Number(timeout)) || Number(timeout) < 1) {
    errors.timeout_seconds = 'Timeout must be a positive integer'
  }
  const onUnknown = config.value.on_unknown
  if (onUnknown && !ON_UNKNOWN_OPTIONS.includes(onUnknown)) {
    errors.on_unknown = `Invalid on_unknown ${onUnknown}; expected one of ${ON_UNKNOWN_OPTIONS.join(', ')}`
  }
  const authMode = credentials.value.auth_mode
  if (authMode && !AUTH_MODE_OPTIONS.includes(authMode)) {
    errors.auth_mode = `Invalid auth_mode ${authMode}; expected one of ${AUTH_MODE_OPTIONS.join(', ')}`
  }
  if (authMode === 'bearer' && !credentials.value.token) {
    errors.token = 'Bearer token is required'
  }
  if (authMode === 'basic') {
    if (!credentials.value.username) errors.username = 'Username is required'
    if (!credentials.value.password) errors.password = 'Password is required'
  }
  if (authMode === 'api_key' && !credentials.value.api_key) {
    errors.api_key = 'API key is required'
  }
  if (config.value.advanced_json && config.value.advanced_json.trim()) {
    try {
      JSON.parse(config.value.advanced_json)
      errors.advanced_json = ''
    } catch {
      errors.advanced_json = 'Advanced JSON is not valid JSON'
    }
  }

  return !Object.values(errors).some(v => v)
}

watch(
  () => ({ ...credentials.value }),
  () => { credsDirty.value = true },
)

defineExpose({ validate })
</script>
