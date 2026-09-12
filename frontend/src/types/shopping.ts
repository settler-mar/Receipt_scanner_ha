// Shopping list (ROADMAP.md Крок 2). Kept out of types/api.ts so it could be
// built alongside Крок 1.

export type ShoppingStatus = 'pending' | 'bought' | 'cancelled'

export interface ShoppingItem {
  id: string
  raw_text: string
  normalized_name: string
  category_id: string | null
  barcode: string | null
  qty: number
  unit: string | null
  status: ShoppingStatus
  note: string | null
  auto_matched: boolean
  bought_receipt_item_id: string | null
  added_at: string
  bought_at: string | null
}

export interface ShoppingItemCreate {
  raw_text: string
  qty?: number
  unit?: string | null
  note?: string | null
  barcode?: string | null
  category_id?: string | null
}

export interface SuggestStoreItem {
  raw_text: string
  last_price: number | null
  unit: string | null
  last_seen_at: string | null
}

export interface SuggestStore {
  store_code: string
  store_name: string
  total_items: number
  covered_items: number
  approx_basket: number
  last_purchase_at: string | null
  items: SuggestStoreItem[]
}

export interface LiveSuggestItem {
  raw_text: string
  query: string
  query_source: 'cache' | 'ai' | 'fallback'
  matched_name: string | null
  last_price: number | null
  product_url: string | null
}

export interface LiveSuggestStore {
  store_code: string
  store_name: string
  total_items: number
  covered_items: number
  approx_basket: number
  items: LiveSuggestItem[]
}
