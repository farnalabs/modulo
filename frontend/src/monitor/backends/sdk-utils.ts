export function isModuleNotFound(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  return 'code' in error && error.code === 'MODULE_NOT_FOUND'
    || error.message.includes('Cannot find module')
}
