import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'
import RestConnectorConfigForm, { type RestConfigState, type RestCredsState } from '../components/connectors/RestConnectorConfigForm.vue'

function defaultConfig(): RestConfigState {
  return {
    base_url: '',
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

function mountForm(mode: 'add' | 'edit' = 'add') {
  const config = ref(defaultConfig())
  const credentials = ref(defaultCreds())
  const credsDirty = ref(false)
  const wrapper = mount(RestConnectorConfigForm, {
    global: {
      /** nothing extra — i18n lints/plugin come from setup */
    },
    props: {
      mode,
      config: config.value,
      credentials: credentials.value,
      credsDirty: credsDirty.value,
      'onUpdate:config': (v: RestConfigState) => { config.value = v },
      'onUpdate:credentials': (v: RestCredsState) => { credentials.value = v },
      'onUpdate:credsDirty': (v: boolean) => { credsDirty.value = v },
    },
  })
  return { wrapper, config, credentials, credsDirty }
}

function validate(wrapper: ReturnType<typeof mountForm>['wrapper']): boolean {
  return (wrapper.vm as unknown as { validate: () => boolean }).validate()
}

async function validateAndFlush(wrapper: ReturnType<typeof mountForm>['wrapper']) {
  const valid = validate(wrapper)
  await wrapper.vm.$nextTick()
  return valid
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
})
