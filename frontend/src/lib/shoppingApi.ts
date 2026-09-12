// Shopping-list API calls (ROADMAP.md Крок 2). Separate from lib/api.ts so it
// could be built alongside Крок 1; fold into the `api` object on merge if
// preferred. Reuses BASE from lib/api.ts.
import { BASE } from '@/lib/api'
import type {
  LiveSuggestStore,
  ShoppingItem,
  ShoppingItemCreate,
  SuggestStore,
} from '@/types/shopping'

async function req<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const shoppingApi = {
  list: (status?: string) =>
    req<ShoppingItem[]>(`/api/shopping-list${status ? `?status=${status}` : ''}`),
  create: (payload: ShoppingItemCreate) =>
    req<ShoppingItem>('/api/shopping-list', { method: 'POST', body: JSON.stringify(payload) }),
  update: (id: string, payload: Partial<ShoppingItemCreate & { status: string }>) =>
    req<ShoppingItem>(`/api/shopping-list/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  remove: (id: string) => req<void>(`/api/shopping-list/${id}`, { method: 'DELETE' }),
  confirm: (id: string) => req<ShoppingItem>(`/api/shopping-list/${id}/confirm`, { method: 'POST' }),
  reject: (id: string) => req<ShoppingItem>(`/api/shopping-list/${id}/reject`, { method: 'POST' }),
  suggestStore: () => req<SuggestStore[]>('/api/shopping-list/suggest-store'),
  suggestStoreLive: () =>
    req<LiveSuggestStore[]>('/api/shopping-list/suggest-store/live', { method: 'POST' }),
  rescan: () => req<{ matched: ShoppingItem[] }>('/api/shopping-list/rescan', { method: 'POST' }),
}
