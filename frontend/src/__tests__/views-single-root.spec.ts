import { describe, it, expect } from 'vitest'
import fs from 'fs'
import path from 'path'
import { parse } from '@vue/compiler-sfc'

/**
 * Guard test: every routed view must have exactly one root element in its
 * <template>. AppLayout.vue wraps <router-view> in <transition name="page"
 * mode="out-in"> which requires a single root element — multiple roots break
 * the transition and leave the next page blank (FAR-852).
 *
 * We parse the SFC with @vue/compiler-sfc rather than a hand-rolled regex. A
 * regex like /<template>([\s\S]*?)<\/template>/ stops at the FIRST </template>,
 * which is the closer of a nested <template v-if>/<template v-else> block — so a
 * second root element after a nested template block is invisible to the guard
 * (and it mis-handles `>` inside attribute values). Parsing the real template
 * AST makes the count correct.
 */
const viewsDir = path.resolve(__dirname, '../views')

describe('routed views must have a single root element (FAR-852 guard)', () => {
  const viewFiles = fs.readdirSync(viewsDir).filter((f) => f.endsWith('.vue'))

  for (const file of viewFiles) {
    it(`${file} has exactly one root template element`, () => {
      const content = fs.readFileSync(path.join(viewsDir, file), 'utf-8')
      const { descriptor, errors } = parse(content, { filename: file })
      if (errors.length) {
        throw new Error(`Failed to parse ${file}: ${errors.map((e) => e.message).join('; ')}`)
      }
      const templateAst = descriptor.template?.ast
      if (!templateAst) {
        // No template block (script-only component) — skip
        return
      }
      // NodeTypes.ELEMENT === 1. Root-level comments / text / interpolations are
      // ignored; only element nodes count as a "root". A nested <template v-if>
      // that lives inside the single root is part of that root's subtree and is
      // correctly NOT counted as a separate root.
      const rootElements = templateAst.children.filter((n) => n.type === 1)
      expect(rootElements.length).toBe(1)
    })
  }

  it('flags a view whose template has a second root after a nested template (regression for the old regex guard)', () => {
    const malicious = `
<template>
  <div>
    <template v-if="x">hi</template>
  </div>
  <div>SECOND ROOT</div>
</template>
<script setup lang="ts"></script>
`
    const { descriptor, errors } = parse(malicious, { filename: 'regression.vue' })
    expect(errors).toHaveLength(0)
    const rootElements = descriptor.template!.ast!.children.filter((n) => n.type === 1)
    expect(rootElements.length).toBe(2)
  })
})
