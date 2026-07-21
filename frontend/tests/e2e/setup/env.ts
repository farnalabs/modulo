export interface TestEnv {
  name: 'local' | 'staging' | 'app'
  credentials: {
    admin: { email: string; password: string }
    demo: { email: string; password: string }
    loginFormEmailSelector: string
    loginFormPasswordSelector: string
  }
}

const FORM_SELECTORS = {
  email: 'input[type="text"]',
  password: 'input[type="password"]',
}

const ENVS: Record<string, TestEnv> = {
  local: {
    name: 'local',
    credentials: {
      admin: { email: 'admin@example.com', password: 'password123' },
      demo: { email: 'demo', password: 'demo' },
      loginFormEmailSelector: FORM_SELECTORS.email,
      loginFormPasswordSelector: FORM_SELECTORS.password,
    },
  },
  staging: {
    name: 'staging',
    credentials: {
      admin: { email: process.env.E2E_ADMIN_EMAIL || 'admin@demo.modulo', password: process.env.E2E_ADMIN_PASSWORD || 'admin123' },
      demo: { email: 'demo', password: 'demo' },
      loginFormEmailSelector: FORM_SELECTORS.email,
      loginFormPasswordSelector: FORM_SELECTORS.password,
    },
  },
  app: {
    name: 'app',
    credentials: {
      admin: { email: 'admin@modulo.run', password: 'admin123' },
      demo: { email: 'demo', password: 'demo' },
      loginFormEmailSelector: FORM_SELECTORS.email,
      loginFormPasswordSelector: FORM_SELECTORS.password,
    },
  },
}

export const BASE_URLS: Record<string, string> = {
  local: 'http://127.0.0.1:5173',
  staging: 'https://staging.modulo.run',
  app: 'https://app.modulo.run',
}

export function getTarget(): string {
  return (process.env.E2E_TARGET || 'local').toLowerCase()
}

export function getBaseUrl(target?: string): string {
  const t = target ?? getTarget()
  return process.env.E2E_BASE_URL || BASE_URLS[t] || BASE_URLS.local
}

export function getTestEnv(): TestEnv {
  const target = getTarget()
  if (!ENVS[target]) {
    console.warn(`[env] Unknown E2E_TARGET "${process.env.E2E_TARGET}", falling back to "local". Valid values: ${Object.keys(ENVS).join(', ')}`)
  }
  return ENVS[target] || ENVS.local
}
