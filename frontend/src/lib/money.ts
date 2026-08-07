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
  const code = currencyCode ? currencyCode.trim().toUpperCase() : 'USD'
  try {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: code,
      currencyDisplay: 'narrowSymbol',
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(safe)
  } catch {
    return `${currencySymbolFor(code)}${safe.toFixed(digits)}`
  }
}
