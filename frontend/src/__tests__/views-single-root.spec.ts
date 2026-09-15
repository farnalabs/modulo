import { describe, it, expect } from 'vitest'
import fs from 'fs'
import path from 'path'
import { parse } from '@vue/compiler-sfc'
import { baseParse, NodeTypes } from '@vue/compiler-dom'

/**
 * Guard test: every routed view must have exactly one root element in its
 * <template>. AppLayout.vue wraps <router-view> in <transition name="page"
 * mode="out-in"> which requires a single root element — multiple roots break
 * the transition and leave the next page blank (FAR-852).
 *
 * We parse the SFC with @vue/compiler-sfc (the same tooling Vue itself uses)
 * so the guard sees the real top-level <template> block, not the first
 * `</template>` closer of a nested `<template v-if>`/`<template v-else>`
 * block. Root elements are then counted from the compiled template AST, which
 * is robust to tags whose attribute values contain `>` and to self-closing
 * tags.
 */
const viewsDir = path.resolve(__dirname, '../views')

/**
 * Count the number of meaningful top-level nodes inside a template block.
 * Whitespace-only text and HTML comments are ignored so that a single wrapping
 * root element (with surrounding whitespace) still counts as exactly one root.
 */
function countRootElements(templateContent: string): number {
  const ast = baseParse(templateContent, { comments: true })
  return ast.children.filter((node) => {
    if (node.type === NodeTypes.COMMENT) return false
    if (node.type === NodeTypes.TEXT) return node.content.trim().length > 0
    return true
  }).length
}

describe('routed views must have a single root element (FAR-852 guard)', () => {
  const viewFiles = fs.readdirSync(viewsDir).filter((f) => f.endsWith('.vue'))

  for (const file of viewFiles) {
    it(`${file} has exactly one root template element`, () => {
      const content = fs.readFileSync(path.join(viewsDir, file), 'utf-8')
      const { descriptor } = parse(content, { filename: file })
      const template = descriptor.template
      if (!template) {
        // No template block (script-only component) — skip
        return
      }
      const rootCount = countRootElements(template.content)
      expect(rootCount).toBe(1)
    })
  }
})
