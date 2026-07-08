import type { Plugin } from 'vite'
import { createInstrumenter } from 'istanbul-lib-instrument'

export default function coveragePlugin(): Plugin {
  return {
    name: 'vite-coverage',
    enforce: 'post',
    transform(code: string, id: string) {
      if (!process.env.VITE_COVERAGE) return
      if (!id.includes('/src/') || id.includes('node_modules')) return

      try {
        const instrumenter = createInstrumenter({
          esModules: true,
          produceSourceMap: true,
          autoWrap: true,
          preserveComments: true,
        })
        return { code: instrumenter.instrumentSync(code, id), map: null }
      } catch {
        return
      }
    },
  }
}
