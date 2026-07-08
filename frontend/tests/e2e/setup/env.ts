export interface TestEnv {
  name: 'local' | 'staging' | 'app'
  credentials: {
    admin: { email: string; password: string }
    demo: { email: string; password: string }
    loginFormEmailSelector: string
    loginFormPasswordSelector: string
  }
}

const ENVS: Record<string, TestEnv> = {
  local: {
    name: 'local',
    credentials: {
      admin: { email: 'admin@example.com', password: 'password123' },
      demo: { email: 'demo', password: 'demo' },
      loginFormEmailSelector: 'input[type="text"]',
      loginFormPasswordSelector: 'input[type="password"]',
    },
  },
  staging: {
    name: 'staging',
    credentials: {
      admin: { email: 'admin@demo.modulo', password: 'admin123' },
      demo: { email: 'demo', password: 'demo' },
      loginFormEmailSelector: 'input[type="text"]',
      loginFormPasswordSelector: 'input[type="password"]',
    },
  },
  app: {
    name: 'app',
    credentials: {
      admin: { email: 'admin@modulo.run', password: 'admin123' },
      demo: { email: 'demo', password: 'demo' },
      loginFormEmailSelector: 'input[type="text"]',
      loginFormPasswordSelector: 'input[type="password"]',
    },
  },
}

export function getTestEnv(): TestEnv {
  const raw = process.env.E2E_TARGET || 'local'
  const target = raw.toLowerCase()
  if (!ENVS[target]) {
    console.warn(`[env] Unknown E2E_TARGET "${raw}", falling back to "local". Valid values: ${Object.keys(ENVS).join(', ')}`)
  }
  return ENVS[target] || ENVS.local
}
