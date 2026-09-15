/**
 * Guard test: prevents direct imports of primevue/select or primevue/dropdown
 * outside of AppSelect.vue. All usages should go through the shared wrapper
 * to ensure appendTo defaults to 'self' (FAR-851, FAR-869).
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'fs'
import { join, relative, extname } from 'path'

const SRC_DIR = join(__dirname, '..')
const ALLOWED_FILE = 'AppSelect.vue'

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
    const relPath = relative(SRC_DIR, file)
    if (relPath.includes(ALLOWED_FILE)) continue

    const content = readFileSync(file, 'utf-8')
    if (/from\s+['"]primevue\/select['"]/.test(content) || /from\s+['"]primevue\/dropdown['"]/.test(content)) {
      violations.push(relPath)
    }
  }

  it('should have no direct primevue/select or primevue/dropdown imports outside AppSelect.vue', () => {
    expect(violations).toEqual([])
  })
})
