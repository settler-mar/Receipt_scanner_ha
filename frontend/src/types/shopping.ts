// Shopping list (ROADMAP.md Крок 2). Kept out of types/api.ts so it could be
// built alongside Крок 1.

export type ShoppingStatus = 'pending' | 'bought' | 'cancelled'

export interface ShoppingItem {
  id: string
  raw_text: string
  normalized_name: string
  category_id: string | null
  category_name: string | null
  category_group?: string | null
  store_class?: string | null
  store_class_name?: string | null
  target_store_code: string | null
  tag: string | null
  barcode: string | null
  qty: number
  unit: string | null
  status: ShoppingStatus
  note: string | null
  auto_matched: boolean
  bought_receipt_item_id: string | null
  added_at: string
  added_method: string
  added_by: string | null
  bought_at: string | null
  bought_store_name: string | null
  bought_receipt_id: string | null
  bought_receipt_url: string | null
  bought_purchased_at: string | null
}

export interface ShoppingItemCreate {
  raw_text: string
  qty?: number
  unit?: string | null
  note?: string | null
  barcode?: string | null
  category_id?: string | null
  target_store_code?: string | null
  tag?: string | null
}

export interface ShoppingArchiveItem {
  shopping_item_id: string
  list_name: string
  qty: number
  unit: string | null
  receipt_item_name: string | null
  receipt_item_total: number | null
}

export interface ShoppingArchiveReceipt {
  receipt_id: string | null
  purchased_at: string | null
  shop_name: string | null
  store_code: string | null
  total_amount: number | null
  currency: string | null
  receipt_url: string | null
  items: ShoppingArchiveItem[]
}

export interface ShoppingMatchCandidate {
  id: string
  shopping_list_item_id: string
  receipt_item_id: string
  receipt_id: string
  list_name: string
  receipt_item_name: string
  confidence: number
  method: string
  explanation: string | null
  status: string
  created_at: string
}

export type ShoppingTripItemStatus = 'planned' | 'in_cart' | 'bought' | 'skipped'

export interface ShoppingTripItem {
  id: string
  trip_id: string
  shopping_list_item_id: string
  raw_text: string
  qty: number
  unit: string | null
  note: string | null
  status: ShoppingTripItemStatus
  created_at: string
}

export interface ShoppingTrip {
  id: string
  store_code: string | null
  owner_name: string
  status: 'active' | 'completed' | 'cancelled'
  created_at: string
  last_activity_at: string
  expires_at: string
  completed_at: string | null
  items: ShoppingTripItem[]
}

export interface ShoppingRoutine {
  normalized_name: string
  display_name: string
  purchase_count: number
  interval_days: number
  last_purchased_at: string
  due_at: string
  days_overdue: number
  ignored: boolean
  postponed_until: string | null
  median_qty: number
  unit: string | null
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
