// Shared "why does this bank transaction never get a receipt" labels — used
// both by the manual per-transaction "Ігнорувати як…" picker
// (BankTransactionsView.vue) and as the target a rule branch can assign
// (BankTransactionRulesView.vue), so the two stay in sync.
export const IGNORE_PRESETS = [
  { value: 'utilities', label: 'Комунальні' },
  { value: 'rent', label: 'Оренда' },
  { value: 'phone_topup', label: 'Поповнення телефону' },
  { value: 'internet', label: 'Інтернет / зв’язок' },
  { value: 'pocket_money', label: 'Кишенькові витрати' },
  { value: 'children', label: 'Діти' },
  { value: 'parents', label: 'Батьки' },
  { value: 'transfer', label: 'Переказ іншій людині' },
  { value: 'cash_withdrawal', label: 'Зняття готівки' },
  { value: 'subscription', label: 'Підписка/сервіс' },
  { value: 'taxi_transport', label: 'Таксі/транспорт' },
  { value: 'cafe_restaurant', label: 'Кафе/ресторан' },
  { value: 'medicine', label: 'Аптека/медицина' },
  { value: 'other', label: 'Інше' },
]
