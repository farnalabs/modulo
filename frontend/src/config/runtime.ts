export interface AutoLoginConfig {
  username: string
  password: string
}

export function getAutoLoginConfig(): AutoLoginConfig | undefined {
  const config = window.__MODULO_CONFIG__?.autoLogin
  if (
    !config
    || typeof config.username !== 'string'
    || typeof config.password !== 'string'
    || !config.username
    || !config.password
  ) {
    return undefined
  }
  return { username: config.username, password: config.password }
}
