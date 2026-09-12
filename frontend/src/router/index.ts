import { createRouter, createWebHashHistory } from 'vue-router'

const router = createRouter({
  // Hash routing leaves the real pathname at the Home Assistant Ingress root,
  // so relative API calls retain its dynamic authentication prefix.
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: () => import('@/views/DashboardView.vue') },
    { path: '/w/:widgetKey', name: 'widget-host', component: () => import('@/views/WidgetHostView.vue') },
    { path: '/accounts', name: 'accounts', component: () => import('@/views/AccountsView.vue') },
    {
      path: '/accounts/:id',
      name: 'account-detail',
      component: () => import('@/views/AccountDetailView.vue'),
      props: true,
    },
    { path: '/receipts', name: 'receipts', component: () => import('@/views/ReceiptsView.vue') },
    { path: '/products', name: 'products', component: () => import('@/views/ProductsView.vue') },
    { path: '/shopping-list', name: 'shopping-list', component: () => import('@/views/ShoppingListView.vue') },
    { path: '/categories', name: 'categories', component: () => import('@/views/CategoriesView.vue') },
    {
      path: '/store-category',
      name: 'store-category',
      component: () => import('@/views/StoreCategoryView.vue'),
    },
    { path: '/warranties', name: 'warranties', component: () => import('@/views/WarrantiesView.vue') },
    { path: '/inbox', name: 'inbox', component: () => import('@/views/InboxView.vue') },
    { path: '/duplicates', name: 'duplicates', component: () => import('@/views/DuplicatesView.vue') },
    // Settings is one page per sidebar entry: `group` decides which sections
    // render, and the page's own tree navigates inside that group.
    { path: '/settings', redirect: '/settings/ai-providers' },
    {
      path: '/settings/:group',
      name: 'settings',
      component: () => import('@/views/SettingsView.vue'),
      props: true,
    },
    { path: '/receipts/new', name: 'receipt-new', component: () => import('@/views/AddReceiptView.vue') },
    {
      path: '/receipts/:id',
      name: 'receipt-detail',
      component: () => import('@/views/ReceiptDetailView.vue'),
      props: true,
    },
  ],
})

export default router
