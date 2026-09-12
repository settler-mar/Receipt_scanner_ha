import { onMounted, ref } from 'vue'
import { api } from '@/lib/api'
import type { ShopSuggestion } from '@/types/api'

/** Backs the "Магазин"/"Адреса" datalists on the manual-entry and edit-receipt
 * forms — picking a name from the dropdown fills the address too, so the same
 * physical store ends up spelled identically everywhere instead of drifting
 * into near-duplicates across manual entries (see api/receipts.py::list_shops).
 */
export function useShopSuggestions() {
  const suggestions = ref<ShopSuggestion[]>([])

  onMounted(async () => {
    suggestions.value = await api.receipts.shops()
  })

  function shopNames(): string[] {
    const seen = new Set<string>()
    const names: string[] = []
    for (const s of suggestions.value) {
      if (!seen.has(s.shop_name)) {
        seen.add(s.shop_name)
        names.push(s.shop_name)
      }
    }
    return names
  }

  /** Every distinct address seen for this shop name — e.g. all ATB branch
   * addresses under the shop_name "ПРОДУКТИ 855" — for the address field's own
   * datalist, scoped live as the user types/picks a name. */
  function addressesFor(shopName: string): string[] {
    const seen = new Set<string>()
    const addresses: string[] = []
    for (const s of suggestions.value) {
      if (s.shop_name === shopName && s.shop_address && !seen.has(s.shop_address)) {
        seen.add(s.shop_address)
        addresses.push(s.shop_address)
      }
    }
    return addresses
  }

  /** The most-used address for this shop name (suggestions are already sorted
   * by count desc from the backend) — the auto-fill guess; wrong branch is one
   * click away via the address field's own datalist. */
  function bestAddressFor(shopName: string): string | null {
    const match = suggestions.value.find((s) => s.shop_name === shopName && s.shop_address)
    return match?.shop_address ?? null
  }

  return { suggestions, shopNames, addressesFor, bestAddressFor }
}
