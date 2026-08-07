export const CURRENCY_SYMBOLS: Record<string, string> = {
  USD: '$',
  EUR: '€',
  GBP: '£',
}

export function currencySymbolFor(currencyCode?: string | null): string {
  const code = currencyCode ? currencyCode.trim().toUpperCase() : ''
  return CURRENCY_SYMBOLS[code] ?? '$'
}

export function formatMoney(amount: number, currencyCode?: string | null, digits = 2): string {
  const n = Number(amount)
  const safe = Number.isFinite(n) ? n : 0
  return `${currencySymbolFor(currencyCode)}${safe.toFixed(digits)}`
}
