import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const VIEW_PATH = resolve(__dirname, '../views/AdminRunnersView.vue')

function readViewSource(): string {
  return readFileSync(VIEW_PATH, 'utf-8')
}

describe('AdminRunnersView layout primitives', () => {
  it('passes runner tabs through PageHeader instead of rendering PageTabs directly', () => {
    const source = readViewSource()

    expect(source).toContain('<PageHeader')
    expect(source).toContain(':tabs="tabs"')
    expect(source).not.toContain('<PageTabs')
    expect(source).not.toContain("import PageTabs from '../components/PageTabs.vue'")
  })
})
