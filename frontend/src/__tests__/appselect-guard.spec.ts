/**
 * Guard test: prevents direct imports of primevue/select or primevue/dropdown
 * outside of AppSelect.vue. All usages should go through the shared wrapper
 * to ensure appendTo defaults to 'self' (FAR-851, FAR-869).
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'fs'
import { join, relative, extname, sep } from 'path'

const SRC_DIR = join(__dirname, '..')
const ALLOWED_FILE = 'AppSelect.vue'

/**
 * Test files that must import the raw primitive rather than the wrapper,
 * because their subject IS the primitive. `primevue-component-dom.spec.ts`
 * mounts each component the app uses (including PrimeVue's Select) to pin its
 * rendered DOM across a dependency upgrade (FAR-2205); routing it through
 * AppSelect would test the wrapper instead of the component. Application code
 * is still covered by the scan below.
 */
const ALLOWED_TEST_FILES = new Set(['__tests__/primevue-component-dom.spec.ts'])

function walk(dir: string): string[] {
  const results: string[] = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      results.push(...walk(full))
    } else if (extname(full) === '.vue' || extname(full) === '.ts') {
      results.push(full)
    }
  }
  return results
}

describe('No direct primevue/select or primevue/dropdown imports', () => {
  const files = walk(SRC_DIR)
  const violations: string[] = []

  for (const file of files) {
    const relPath = relative(SRC_DIR, file).split(sep).join('/')
    if (relPath.includes(ALLOWED_FILE)) continue
    if (ALLOWED_TEST_FILES.has(relPath)) continue

    const content = readFileSync(file, 'utf-8')
    if (/from\s+['"]primevue\/select['"]/.test(content) || /from\s+['"]primevue\/dropdown['"]/.test(content)) {
      violations.push(relPath)
    }
  }

  it('should have no direct primevue/select or primevue/dropdown imports outside AppSelect.vue', () => {
    expect(violations).toEqual([])
  })
})
