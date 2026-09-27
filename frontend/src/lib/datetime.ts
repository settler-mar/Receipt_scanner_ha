// All timestamps from the API are UTC (ISO strings with a "+00:00"/"Z" offset).
// The app is used from one household in Ukraine, so everything is displayed in
// Europe/Kyiv regardless of the device's own timezone — never rely on the
// browser locale's zone.

const TZ = 'Europe/Kyiv'

/** "27.08.2026, 11:40:33" in Kyiv time. */
export function fmtDateTime(v: string | null | undefined): string {
  if (!v) return '—'
  return new Date(v).toLocaleString('uk-UA', { timeZone: TZ })
}

/** "27.08 11:40" in Kyiv time — no year, no seconds, for dense UI labels. */
export function fmtDayTime(v: string | null | undefined): string {
  if (!v) return '—'
  return new Date(v)
    .toLocaleString('uk-UA', {
      timeZone: TZ,
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
    .replace(',', '')
}

/** "27.08.2026" in Kyiv time. */
export function fmtDate(v: string | null | undefined): string {
  if (!v) return '—'
  return new Date(v).toLocaleDateString('uk-UA', { timeZone: TZ })
}

/** UTC ISO instant → "YYYY-MM-DDTHH:mm" Kyiv wall-clock, for <input type="datetime-local">. */
export function isoToKyivInput(v: string): string {
  // sv-SE formats as "YYYY-MM-DD HH:mm:ss"; take date + HH:mm.
  const s = new Date(v).toLocaleString('sv-SE', { timeZone: TZ })
  return s.slice(0, 16).replace(' ', 'T')
}

/** Now, as a "YYYY-MM-DDTHH:mm" Kyiv wall-clock string. */
export function nowKyivInput(): string {
  return isoToKyivInput(new Date().toISOString())
}

/** "YYYY-MM-DDTHH:mm" entered as Kyiv wall-clock → UTC ISO instant. */
export function kyivInputToISO(local: string): string {
  // Read the wall-clock fields as if they were UTC, then subtract Kyiv's offset
  // at that instant to get the real UTC time.
  const asUtc = new Date(local.length === 16 ? local + ':00Z' : local + 'Z')
  const offsetMin =
    (new Date(asUtc.toLocaleString('en-US', { timeZone: TZ })).getTime() -
      new Date(asUtc.toLocaleString('en-US', { timeZone: 'UTC' })).getTime()) /
    60000
  return new Date(asUtc.getTime() - offsetMin * 60000).toISOString()
}

/** Uptime seconds → "3 д 4 год", "4 год 12 хв", "12 хв", "45 с". */
export function fmtUptime(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return '—'
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (d) return `${d} д ${h} год`
  if (h) return `${h} год ${m} хв`
  if (m) return `${m} хв`
  return `${seconds} с`
}
