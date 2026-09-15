import { describe, it, expect } from 'vitest'
import fs from 'fs'
import path from 'path'

/**
 * Guard test: every routed view must have exactly one root element in its
 * <template>. AppLayout.vue wraps <router-view> in <transition name="page"
 * mode="out-in"> which requires a single root element — multiple roots break
 * the transition and leave the next page blank (FAR-852).
 */
const viewsDir = path.resolve(__dirname, '../views')

/**
 * Count the number of top-level root elements inside a <template> block.
 * Skips HTML comments. Handles self-closing and normal open/close tags.
 * Tracks depth so nested elements inside a root don't inflate the count.
 */
function countRootElements(templateContent: string): number {
  const cleaned = templateContent.replace(/<!--[\s\S]*?-->/g, '')
  let depth = 0
  let rootCount = 0
  let i = 0

  while (i < cleaned.length) {
    if (cleaned[i] !== '<') {
      i++
      continue
    }

    // Closing tag at depth 0 shouldn't happen in well-formed template,
    // but decrement depth if it does
    if (cleaned[i + 1] === '/') {
      const closeMatch = cleaned.slice(i).match(/^<\/([a-zA-Z][a-zA-Z0-9-]*)/)
      if (closeMatch) {
        depth = Math.max(0, depth - 1)
        i += closeMatch[0].length
        continue
      }
      i++
      continue
    }

    // Opening tag (or self-closing)
  const openMatch = cleaned.slice(i).match(/^<([a-zA-Z][a-zA-Z0-9-]*)/)
      if (openMatch) {
        // Check if self-closing (ends with />)
        const selfClosing = cleaned.slice(i).match(/^<[^>]*\/>/)
      if (depth === 0) {
        rootCount++
      }
      if (!selfClosing) {
        depth++
      }
      i += openMatch[0].length
      continue
    }

    i++
  }

  return rootCount
}

describe('routed views must have a single root element (FAR-852 guard)', () => {
  const viewFiles = fs.readdirSync(viewsDir).filter((f) => f.endsWith('.vue'))

  for (const file of viewFiles) {
    it(`${file} has exactly one root template element`, () => {
      const content = fs.readFileSync(path.join(viewsDir, file), 'utf-8')
      const templateMatch = content.match(/<template>([\s\S]*?)<\/template>/)
      if (!templateMatch) {
        // No template block (script-only component) — skip
        return
      }
      const rootCount = countRootElements(templateMatch[1])
      expect(rootCount).toBe(1)
    })
  }
})
