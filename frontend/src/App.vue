<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

import { api } from '@/lib/api'
import { fmtDateTime, fmtUptime } from '@/lib/datetime'
import type { Health, NavBadge } from '@/types/api'

// A single widget embedded elsewhere (Home Assistant iframe card) renders
// chrome-less — no sidebar, no app frame.
const route = useRoute()
const mobileNavOpen = ref(false)

watch(
  () => route.fullPath,
  () => {
    mobileNavOpen.value = false
    // Leaving a page usually means something there was just handled — re-count
    // so a resolved problem's badge disappears right away.
    void loadBadges()
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
      { to: '/bank-transactions', label: 'Банк', icon: '🏦' },
    ],
  },
  {
    title: 'Каталог',
    items: [
      { to: '/products/live', label: 'Пошук у магазинах', icon: '🔎' },
      { to: '/products/history', label: 'Історія покупок', icon: '🧾' },
      { to: '/categories', label: 'Категорії', icon: '🏷️' },
      { to: '/warranties', label: 'Гарантії', icon: '🛡️' },
    ],
  },
  {
    title: 'Система',
    items: [
      { to: '/inbox', label: 'Інбокс', icon: '📥' },
      { to: '/duplicates', label: 'Дублі', icon: '🧬' },
      { to: '/logs', label: 'Журнал', icon: '📜' },
      { to: '/accounts', label: 'Застосунки', icon: '🔌' },
      {
        to: '/settings',
        label: 'Налаштування',
        icon: '⚙️',
        children: [
          { to: '/settings/ai-providers', label: 'AI · сервіси' },
          { to: '/settings/ai-routing', label: 'AI · маршрути' },
          { to: '/settings/ai-prompts', label: 'AI · промти' },
          { to: '/settings/integrations', label: 'Інтеграції' },
          { to: '/settings/scanner', label: 'Сканування' },
          { to: '/settings/storage', label: 'Зберігання' },
          { to: '/raw-responses', label: 'Сырые ответы' },
          { to: '/settings/shopping', label: 'Список покупок' },
        ],
      },
    ],
  },
]

// Backend build + uptime, shown at the bottom of the sidebar. The clock ticks
// locally off started_at; the poll only has to catch a restart (uptime reset).
const health = ref<Health | null>(null)
const now = ref(Date.now())
let poll: number | undefined
let tick: number | undefined

const uptime = computed(() => {
  if (!health.value) return '—'
  const started = new Date(health.value.started_at).getTime()
  return fmtUptime(Math.floor((now.value - started) / 1000))
})

async function loadHealth() {
  try {
    health.value = await api.health.get()
  } catch {
    health.value = null
  }
}

// Problems used to be invisible until someone opened the right page: a chain
// whose nightly collection 401s still showed "востаннє зібрано" and a green
// account. Badges surface them from anywhere — but only while they are *open*:
// the backend counts current state (a failing integration, a document awaiting
// a decision), never past events, so a fixed problem stops being advertised.
const badges = ref<NavBadge[]>([])
const badgeFor = (to: string) => badges.value.find((b) => b.to === to)

async function loadBadges() {
  try {
    badges.value = (await api.nav.badges()).items
  } catch {
    badges.value = []
  }
}

onMounted(() => {
  if (route.name === 'widget-host') return
  void loadHealth()
  void loadBadges()
  poll = window.setInterval(() => {
    void loadHealth()
    void loadBadges()
  }, 5 * 60 * 1000)
  tick = window.setInterval(() => (now.value = Date.now()), 30 * 1000)
})

onUnmounted(() => {
  if (poll) window.clearInterval(poll)
  if (tick) window.clearInterval(tick)
})

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
            <span
              v-if="badgeFor(item.to)"
              class="nav-badge"
              :class="`nav-badge-${badgeFor(item.to)!.tone}`"
              :title="`${badgeFor(item.to)!.hint}: ${badgeFor(item.to)!.count}`"
            >
              {{ badgeFor(item.to)!.count }}
            </span>
          </RouterLink>
          <RouterLink
            v-for="child in item.children ?? []"
            :key="child.to"
            :to="child.to"
            class="nav-link nav-sublink"
            :class="{ 'nav-link-active': $route.path === child.to }"
          >
            <span>{{ child.label }}</span>
            <span
              v-if="badgeFor(child.to)"
              class="nav-badge"
              :class="`nav-badge-${badgeFor(child.to)!.tone}`"
              :title="`${badgeFor(child.to)!.hint}: ${badgeFor(child.to)!.count}`"
            >
              {{ badgeFor(child.to)!.count }}
            </span>
          </RouterLink>
        </template>
      </nav>
      <div class="sidebar-footer">
        <template v-if="health">
          <span class="sidebar-footer-row" :title="`Старт: ${fmtDateTime(health.started_at)}`">
            <span>⏱</span><span>{{ uptime }}</span>
          </span>
          <span class="sidebar-footer-row"><span>🏷</span><span>{{ health.version }}</span></span>
        </template>
        <span v-else class="sidebar-footer-row sidebar-footer-down">
          <span>⚠</span><span>Бекенд недоступний</span>
        </span>
      </div>
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
