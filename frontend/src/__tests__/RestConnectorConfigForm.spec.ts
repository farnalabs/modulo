import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { ref } from 'vue'
import RestConnectorConfigForm, {
  REST_FLAT_FIELDS,
  REST_ADVANCED_FIELDS,
  type RestConfigState,
  type RestCredsState,
} from '../components/connectors/RestConnectorConfigForm.vue'

function defaultConfig(): RestConfigState {
  return {
    base_url: 'https://api.example.com',
    method: 'GET',
    timeout_seconds: 30,
    verify_tls: true,
    on_unknown: 'fail_open',
    records_path: '',
    allowed_hosts: '',
    advanced_json: '',
  }
}

function defaultCreds(): RestCredsState {
  return {
    auth_mode: 'bearer',
    token: '',
    username: '',
    password: '',
    api_key: '',
    apiKeyIn: 'header',
    header_name: '',
    query_param_name: '',
  }
}

function mountForm(mode: 'add' | 'edit' = 'add', initialCreds?: Partial<RestCredsState>) {
  const config = ref(defaultConfig())
  const credentials = ref(initialCreds ? { ...defaultCreds(), ...initialCreds } : defaultCreds())
  const credsDirty = ref(false)
  const credsIdentityDirty = ref(false)
  const wrapper = mount(RestConnectorConfigForm, {
    global: {
      /** nothing extra — i18n/lints/plugin come from setup */
    },
    props: {
      mode,
      config: config.value,
      credentials: credentials.value,
      credsDirty: credsDirty.value,
      credsIdentityDirty: credsIdentityDirty.value,
      'onUpdate:config': (v: RestConfigState) => { config.value = v },
      'onUpdate:credentials': (v: RestCredsState) => { credentials.value = v },
      'onUpdate:credsDirty': (v: boolean) => { credsDirty.value = v },
      'onUpdate:credsIdentityDirty': (v: boolean) => { credsIdentityDirty.value = v },
    },
  })
  return { wrapper, config, credentials, credsDirty, credsIdentityDirty }
}

function validate(wrapper: ReturnType<typeof mountForm>['wrapper']): boolean {
  return (wrapper.vm as unknown as { validate: () => boolean }).validate()
}

async function validateAndFlush(wrapper: ReturnType<typeof mountForm>['wrapper']) {
  const valid = validate(wrapper)
  await wrapper.vm.$nextTick()
  return valid
}

// Kept in sync with the backend `config_schema` for the Generic REST connector
// (backend/src/modulo/core/library/integrations/definitions.py). Reads the live
// Python file so drift in either side fails the parity guard (FAR-466).
function readRestConfigSchemaFields(): { fields: string[]; advanced: string[] } {
  const specPath = resolve(dirname(fileURLToPath(import.meta.url)), '../../../backend/src/modulo/core/library/integrations/definitions.py')
  const source = readFileSync(specPath, 'utf8')
  const defined = source.indexOf('"config_schema": {')
  const fieldsStart = source.indexOf('"fields": {', defined) + '"fields": {'.length
  const fieldsEnd = source.indexOf('"auth": {', fieldsStart)
  const fieldsBlock = source.slice(fieldsStart, fieldsEnd)
  const fields = [...fieldsBlock.matchAll(/"([a-z0-9_]+)":\s*\{/g)]
    .map(m => m[1])
    // `items` is a nested sub-schema inside allowed_hosts, not a top-level field.
    .filter(k => k !== 'items')
  const advancedStart = source.indexOf('"advanced_fields": [', defined) + '"advanced_fields": ['.length
  const advancedEnd = source.indexOf('],', advancedStart)
  const advancedBlock = source.slice(advancedStart, advancedEnd)
  const advanced = [...advancedBlock.matchAll(/"([a-z0-9_]+)"/g)].map(m => m[1])
  return { fields, advanced }
}

describe('RestConnectorConfigForm', () => {
  it('rejects a missing bearer token (auth profile requirement)', async () => {
    const { wrapper, credentials } = mountForm()
    credentials.value.auth_mode = 'bearer'
    credentials.value.token = ''
    await wrapper.vm.$nextTick()
    expect(await validateAndFlush(wrapper)).toBe(false)
  })

  it('passes when bearer token is set and config is valid', async () => {
    const { wrapper, config, credentials } = mountForm()
    credentials.value.token = 'abc'
    config.value.method = 'GET'
    await wrapper.vm.$nextTick()
    expect(await validateAndFlush(wrapper)).toBe(true)
  })

  it('rejects an invalid on_unknown value with a loud inline error', async () => {
    const { wrapper, config, credentials } = mountForm()
    credentials.value.token = 'abc'
    config.value.on_unknown = 'bogus'
    await wrapper.vm.$nextTick()
    expect(await validateAndFlush(wrapper)).toBe(false)
    expect(wrapper.text()).toContain('Invalid on_unknown')
  })

  it('rejects malformed advanced JSON with a loud inline error', async () => {
    const { wrapper, config, credentials } = mountForm()
    credentials.value.token = 'abc'
    config.value.advanced_json = '{ not json'
    await wrapper.vm.$nextTick()
    expect(await validateAndFlush(wrapper)).toBe(false)
    expect(wrapper.text()).toContain('Advanced JSON is not valid JSON')
  })

  it('marks credentials dirty when the auth profile is edited', async () => {
    const { wrapper, credentials, credsDirty } = mountForm()
    credentials.value.token = 'abc'
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(credsDirty.value).toBe(true)
  })

  it('does NOT mark credentials dirty on a config-only edit (stored secret preserved)', async () => {
    const { wrapper, config, credsDirty, credsIdentityDirty } = mountForm('edit')
    config.value.base_url = 'https://api.example.com/v2'
    config.value.timeout_seconds = 60
    config.value.records_path = 'data.items'
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    // Only operational config changed — the auth section is untouched, so the
    // stored credential must not be re-sent (credsDirty AND credsIdentityDirty
    // must both stay false → the parent sends NO credentials).
    expect(credsDirty.value).toBe(false)
    expect(credsIdentityDirty.value).toBe(false)
  })

  it('marks credsIdentityDirty (NOT credsDirty) on an identity-only edit, and saves without demanding the secret', async () => {
    // Edit-mode prefill echoes the non-secret auth identity (header_name,
    // apiKeyIn, auth_mode) while the secret stays write-only/empty. Editing ONLY
    // that identity must set credsIdentityDirty (so the credentials payload IS
    // re-sent and the backend overlays the new identity) while credsDirty stays
    // false (so the secret-preservation invariant holds and no secret is
    // clobbered), and must not demand a re-entered secret on validate().
    const { wrapper, credsDirty, credsIdentityDirty, credentials } = mountForm('edit', {
      auth_mode: 'api_key',
      api_key: '',
      apiKeyIn: 'header',
      header_name: 'X-API-Key',
      query_param_name: '',
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(credsDirty.value).toBe(false)
    expect(credsIdentityDirty.value).toBe(false)
    credentials.value.header_name = 'X-API-Key-V2'
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    // The identity field changed → credentials ARE sent (credsIdentityDirty),
    // but credsDirty stays false (no secret touched) so the parent re-sends the
    // credentials and the backend preserves the stored secret.
    expect(credsDirty.value).toBe(false)
    expect(credsIdentityDirty.value).toBe(true)
    // credsDirty is false → validate() must pass without a re-entered secret.
    expect(await validateAndFlush(wrapper)).toBe(true)
  })

  it('rejects switching auth_mode to bearer WITHOUT a token (mode change demands the new secret)', async () => {
    // Repro (FAR-466): edit an existing api_key connector, switch the dropdown
    // to bearer, save without typing a token. auth_mode is an IDENTITY field, so
    // the switch flips credsIdentityDirty (NOT credsDirty) and the old
    // `edit && !credsDirty` gate LEFT the secret-required checks suppressed —
    // a silently broken connector. The mode switch must demand a bearer token.
    const { wrapper, credentials } = mountForm('edit', {
      auth_mode: 'api_key',
      api_key: '',
      apiKeyIn: 'header',
      header_name: 'X-API-Key',
      query_param_name: '',
    })
    await wrapper.vm.$nextTick()
    // Untouched edit prefill still validates (no secret demanded yet).
    expect(await validateAndFlush(wrapper)).toBe(true)
    credentials.value.auth_mode = 'bearer'
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    // credsDirty stays false, but the mode changed away from the stored mode →
    // bearer requires a token.
    expect(credentials.value.auth_mode).toBe('bearer')
    expect(await validateAndFlush(wrapper)).toBe(false)
    expect(wrapper.text()).toContain('Bearer token is required')
  })

  it('passes switching auth_mode to bearer WITH the new-mode token', async () => {
    const { wrapper, credentials } = mountForm('edit', {
      auth_mode: 'api_key',
      api_key: '',
      apiKeyIn: 'header',
      header_name: 'X-API-Key',
      query_param_name: '',
    })
    await wrapper.vm.$nextTick()
    credentials.value.auth_mode = 'bearer'
    credentials.value.token = 'abc'
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(await validateAndFlush(wrapper)).toBe(true)
  })

  it('still passes an identity-only edit that does NOT change auth_mode (no secret demanded)', async () => {
    // header_name is an identity field but NOT auth_mode: editing it must keep
    // credsDirty false AND not trip the mode-changed gate, so validate() stays
    // lenient (no re-entered secret required).
    const { wrapper, credsDirty, credsIdentityDirty, credentials } = mountForm('edit', {
      auth_mode: 'api_key',
      api_key: '',
      apiKeyIn: 'header',
      header_name: 'X-API-Key',
      query_param_name: '',
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    credentials.value.header_name = 'X-API-Key-V2'
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(credsDirty.value).toBe(false)
    expect(credsIdentityDirty.value).toBe(true)
    expect(credentials.value.auth_mode).toBe('api_key')
    expect(await validateAndFlush(wrapper)).toBe(true)
  })

  it('renders the three on_unknown options and their help text', async () => {
    const { wrapper } = mountForm()
    await wrapper.vm.$nextTick()
    const select = wrapper.find('[data-testid="rest-connector-on-unknown"]')
    const options = select.findAll('option').map(o => o.text())
    expect(options).toEqual(['fail_open', 'fail_closed', 'off'])
    expect(select.element.getAttribute('aria-describedby')).toContain('restconn-on-unknown-help')
    expect(wrapper.text()).toContain('recover duplicates')
  })

  it('does not clobber the stored secret on an edit prefill round-trip', async () => {
    // Edit-mode prefill echoes the stored config; the secret stays write-only and
    // credsDirty stays false so the credentials payload is never re-sent empty.
    // The identity channel is likewise untouched, so credsIdentityDirty stays
    // false (NO credentials re-sent on an untouched edit).
    const { wrapper, credsDirty, credsIdentityDirty, credentials } = mountForm('edit', {
      auth_mode: 'api_key',
      api_key: 'sk-...',
      apiKeyIn: 'query',
      query_param_name: 'key',
      header_name: 'X-API-Key',
    })
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(credsDirty.value).toBe(false)
    expect(credsIdentityDirty.value).toBe(false)
    expect(credentials.value.auth_mode).toBe('api_key')
  })

  it('does NOT demand a credential when saving an untouched edit-mode prefill', async () => {
    const { wrapper, credentials } = mountForm('edit')
    credentials.value.token = ''
    await wrapper.vm.$nextTick()
    // base_url from defaultConfig is valid, timeout valid — without a re-entered
    // secret the validate() must still pass because credsDirty is false.
    expect(await validateAndFlush(wrapper)).toBe(true)
  })

  it('rejects an empty base_url', async () => {
    const { wrapper, config, credentials } = mountForm()
    credentials.value.token = 'abc'
    config.value.base_url = ''
    await wrapper.vm.$nextTick()
    expect(await validateAndFlush(wrapper)).toBe(false)
    expect(wrapper.text()).toContain('Base URL is required')
  })

  it('rejects an invalid (non-URL) base_url', async () => {
    const { wrapper, config, credentials } = mountForm()
    credentials.value.token = 'abc'
    config.value.base_url = 'not-a-url'
    await wrapper.vm.$nextTick()
    expect(await validateAndFlush(wrapper)).toBe(false)
    expect(wrapper.text()).toContain('Base URL must be a valid URL')
  })

  it('rejects a base_url with a non-http(s) scheme', async () => {
    const { wrapper, config, credentials } = mountForm()
    credentials.value.token = 'abc'
    config.value.base_url = 'mailto:user@example.com'
    await wrapper.vm.$nextTick()
    expect(await validateAndFlush(wrapper)).toBe(false)
    expect(wrapper.text()).toContain('Base URL must be a valid URL')
  })

  it('rejects a non-positive or non-integer timeout', async () => {
    const { wrapper, config, credentials } = mountForm()
    credentials.value.token = 'abc'
    config.value.timeout_seconds = 0
    await wrapper.vm.$nextTick()
    expect(await validateAndFlush(wrapper)).toBe(false)
    config.value.timeout_seconds = 1.5
    await wrapper.vm.$nextTick()
    expect(await validateAndFlush(wrapper)).toBe(false)
  })

  it('keeps the form field lists in parity with the connector config_schema (no drift)', () => {
    const { fields, advanced } = readRestConfigSchemaFields()
    expect(new Set(REST_FLAT_FIELDS)).toEqual(new Set(fields))
    expect(new Set(REST_ADVANCED_FIELDS)).toEqual(new Set(advanced))
  })
})
