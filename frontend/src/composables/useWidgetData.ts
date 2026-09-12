import { onMounted, ref, watch, type Ref } from 'vue'

/**
 * Per-widget async loading. Each dashboard widget owns its own request so the
 * dashboard no longer blocks on one big Promise.all — a slow block (fuel prices,
 * which hits live station APIs) never holds up the rest.
 */
export interface WidgetData<T> {
  data: Ref<T | undefined>
  status: Ref<'loading' | 'ready' | 'error'>
  error: Ref<string>
  reload: () => Promise<void>
}

export function useWidgetData<T>(
  loader: () => Promise<T>,
  watchSource?: () => unknown,
): WidgetData<T> {
  const data = ref<T>() as Ref<T | undefined>
  const status = ref<'loading' | 'ready' | 'error'>('loading')
  const error = ref('')

  async function reload() {
    status.value = 'loading'
    error.value = ''
    try {
      data.value = await loader()
      status.value = 'ready'
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
      status.value = 'error'
    }
  }

  onMounted(reload)
  if (watchSource) watch(watchSource, reload)

  return { data, status, error, reload }
}
