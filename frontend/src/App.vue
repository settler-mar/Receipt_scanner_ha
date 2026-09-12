<script setup lang="ts">
import { ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

// A single widget embedded elsewhere (Home Assistant iframe card) renders
// chrome-less — no sidebar, no app frame.
const route = useRoute()
const mobileNavOpen = ref(false)

watch(
  () => route.fullPath,
  () => {
    mobileNavOpen.value = false
  },
)

interface NavItem {
  to: string
  label: string
  icon: string
  // One level of nesting, used by Settings: the sub-pages show right in the
  // sidebar, and each one renders only its own sections.
  children?: { to: string; label: string }[]
}

interface NavGroup {
  title: string
  items: NavItem[]
}

// Grouped so related destinations sit together and the eye can scan by section.
const groups: NavGroup[] = [
  {
    title: 'Огляд',
    items: [
      { to: '/', label: 'Дашборд', icon: '📊' },
      { to: '/store-category', label: 'Магазини × категорії', icon: '🔀' },
      { to: '/receipts', label: 'Чеки', icon: '🧾' },
      { to: '/shopping-list', label: 'Список покупок', icon: '🛒' },
    ],
  },
  {
    title: 'Каталог',
    items: [
      { to: '/products', label: 'Товари', icon: '📦' },
      { to: '/categories', label: 'Категорії', icon: '🏷️' },
      { to: '/warranties', label: 'Гарантії', icon: '🛡️' },
    ],
  },
  {
    title: 'Система',
    items: [
      { to: '/inbox', label: 'Інбокс', icon: '📥' },
      { to: '/duplicates', label: 'Дублі', icon: '🧬' },
      { to: '/accounts', label: 'Застосунки', icon: '🔌' },
      {
        to: '/settings',
        label: 'Налаштування',
        icon: '⚙️',
        children: [
          { to: '/settings/ai-providers', label: 'AI · сервіси' },
          { to: '/settings/ai-routing', label: 'AI · маршрути' },
          { to: '/settings/integrations', label: 'Інтеграції' },
          { to: '/settings/scanner', label: 'Сканування' },
          { to: '/settings/storage', label: 'Зберігання' },
        ],
      },
    ],
  },
]

// Views worth keeping alive: list-heavy pages the user returns to often.
// Names are inferred from the SFC filenames by @vitejs/plugin-vue.
const keepAlive = ['DashboardView', 'ReceiptsView', 'ProductsView', 'ShoppingListView']
</script>

<template>
  <RouterView v-if="route.name === 'widget-host'" />
  <div v-else class="layout">
    <button
      class="mobile-nav-toggle"
      type="button"
      aria-controls="app-sidebar"
      :aria-expanded="mobileNavOpen"
      :aria-label="mobileNavOpen ? 'Закрити меню' : 'Відкрити меню'"
      @click="mobileNavOpen = !mobileNavOpen"
    >
      {{ mobileNavOpen ? '✕' : '☰' }}
    </button>
    <button
      v-if="mobileNavOpen"
      class="mobile-nav-backdrop"
      type="button"
      aria-label="Закрити меню"
      @click="mobileNavOpen = false"
    />
    <aside id="app-sidebar" class="sidebar" :class="{ 'sidebar-open': mobileNavOpen }">
      <h1>🧾 Receipt Scanner</h1>
      <nav v-for="group in groups" :key="group.title" class="nav-group">
        <span class="nav-group-title">{{ group.title }}</span>
        <template v-for="item in group.items" :key="item.to">
          <RouterLink
            :to="item.to"
            class="nav-link"
            :class="{ 'nav-link-active': item.to === '/' ? $route.path === '/' : $route.path.startsWith(item.to) }"
          >
            <span class="nav-icon">{{ item.icon }}</span>
            <span>{{ item.label }}</span>
          </RouterLink>
          <RouterLink
            v-for="child in item.children ?? []"
            :key="child.to"
            :to="child.to"
            class="nav-link nav-sublink"
            :class="{ 'nav-link-active': $route.path === child.to }"
          >
            <span>{{ child.label }}</span>
          </RouterLink>
        </template>
      </nav>
    </aside>
    <main class="content">
      <RouterView v-slot="{ Component }">
        <KeepAlive :include="keepAlive">
          <component :is="Component" />
        </KeepAlive>
      </RouterView>
    </main>
  </div>
</template>
