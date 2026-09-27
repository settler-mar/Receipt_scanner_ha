import { defineAsyncComponent, type Component } from 'vue'

export interface WidgetMeta {
  key: string
  title: string
  component: Component
  /** Whether the config panel offers this widget a per-widget date-range override. */
  supportsRange: boolean
  /** Whether the dashboard's "враховувати знижку" checkbox changes this widget. */
  supportsDiscount: boolean
  /** Whether the widget keeps its own settings blob in the stored layout. */
  supportsOptions?: boolean
  defaultVisible: boolean
}

// Order here = default dashboard order (before the user reorders in the
// config panel, which persists into app_settings `dashboard.layout`).
export const WIDGETS: WidgetMeta[] = [
  {
    key: 'stat-cards',
    title: 'Загальні показники',
    supportsRange: true,
    supportsDiscount: true,
    defaultVisible: true,
    component: defineAsyncComponent(() => import('./StatCardsWidget.vue')),
  },
  {
    key: 'spend-trend',
    title: 'Витрати за період',
    supportsRange: true,
    supportsDiscount: true,
    supportsOptions: true,
    defaultVisible: true,
    component: defineAsyncComponent(() => import('./SpendTrendWidget.vue')),
  },
  {
    key: 'by-store',
    title: 'Витрати за магазином',
    supportsRange: true,
    supportsDiscount: true,
    defaultVisible: true,
    component: defineAsyncComponent(() => import('./ByStoreWidget.vue')),
  },
  {
    key: 'by-channel',
    title: 'Онлайн / офлайн',
    supportsRange: true,
    supportsDiscount: true,
    defaultVisible: true,
    component: defineAsyncComponent(() => import('./ByChannelWidget.vue')),
  },
  {
    key: 'savings',
    title: 'Заощадження та бонуси',
    supportsRange: true,
    supportsDiscount: true,
    defaultVisible: true,
    component: defineAsyncComponent(() => import('./SavingsWidget.vue')),
  },
  {
    key: 'by-category',
    title: 'Витрати за категоріями',
    supportsRange: true,
    supportsDiscount: true,
    defaultVisible: true,
    component: defineAsyncComponent(() => import('./ByCategoryWidget.vue')),
  },
  {
    key: 'store-category',
    title: 'Магазини × категорії',
    supportsRange: true,
    supportsDiscount: true,
    defaultVisible: false,
    component: defineAsyncComponent(() => import('./StoreCategoryWidget.vue')),
  },
  {
    key: 'fuel-prices',
    title: 'Ціни на паливо',
    supportsRange: false,
    supportsDiscount: true,
    defaultVisible: true,
    component: defineAsyncComponent(() => import('./FuelPricesWidget.vue')),
  },
  {
    key: 'recent-receipts',
    title: 'Останні чеки',
    supportsRange: false,
    supportsDiscount: false,
    defaultVisible: true,
    component: defineAsyncComponent(() => import('./RecentReceiptsWidget.vue')),
  },
]

export const WIDGET_MAP = new Map(WIDGETS.map((w) => [w.key, w]))
