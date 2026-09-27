// Shared store iconography — used by AccountsView's catalog and ReceiptsView's
// table so a given store looks the same everywhere, not redefined per view.
import brsmLogo from '@/assets/brsm-logo.webp'
import venconLogo from '@/assets/vencon-logo.png'
import monobankLogo from '@/assets/monobank-logo.png'

// Direct logo URLs where a proper brand mark beats a tiny favicon.
const STORE_LOGOS: Record<string, string> = {
  metro: 'https://img4.zakaz.ua/own_brands_logos/metro.svg',
  brsm: brsmLogo,
  // Official 192px Foxtrot app icon from the site's favicon set.
  foxtrot: 'https://www.foxtrot.com.ua/dist/images/favicon/android-icon-192x192.png',
  klo: 'https://commons.wikimedia.org/wiki/Special:FilePath/KLO_logo.svg',
  wog: 'https://commons.wikimedia.org/wiki/Special:FilePath/WOG_Logo.svg',
  // The actual mono app icon (rounded black square + wordmark), pulled from
  // the App Store listing — matches what's on the user's home screen, unlike
  // the flat wordmark-only logo used elsewhere.
  monobank: monobankLogo,
  // Official square brand mark; unlike the full wordmark it stays legible at
  // the 16–28 px sizes used in receipt tables and account cards.
  vencon: venconLogo,
}

// Real app/site logos via a favicon service, keyed by each store's own web
// domain — no logo files stored in this repo. `scanner` isn't a real company
// (it's this app's own manual-entry flow), so it keeps an emoji instead.
const STORE_DOMAINS: Record<string, string> = {
  atb: 'atbmarket.com',
  fozzy: 'fozzyshop.ua',
  fora: 'fora.ua',
  silpo: 'silpo.ua',
  epicentrk: 'epicentrk.ua',
  'dnipro-m': 'dnipro-m.ua',
  metro: 'metro-cc.ua',
  wog: 'wog.ua',
  ukrnafta: 'ukrnafta.com',
  bars: 'barsgroup.com.ua',
  upg: 'upg.ua',
  foxtrot: 'foxtrot.com.ua',
  klo: 'klo.ua',
  vencon: 'vencon.ua',
  prom: 'prom.ua',
  rozetka: 'rozetka.com.ua',
  aliexpress: 'aliexpress.com',
  brain: 'brain.com.ua',
  eva: 'eva.ua',
  liki: 'liki.ua',
  tabletki: 'tabletki.ua',
  olx: 'olx.ua',
}

const FALLBACK_ICON: Record<string, string> = { scanner: '🧾' }
export const DEFAULT_ICON = '🏪'

export function storeLogoUrl(code: string): string | null {
  if (STORE_LOGOS[code]) return STORE_LOGOS[code]
  const domain = STORE_DOMAINS[code]
  return domain ? `https://www.google.com/s2/favicons?sz=64&domain=${domain}` : null
}

export function storeFallbackIcon(code: string): string {
  return FALLBACK_ICON[code] ?? DEFAULT_ICON
}
