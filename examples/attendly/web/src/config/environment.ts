// Attendly is served by its own FastAPI process on one origin: the API,
// the WebSocket and this UI share window.location. VITE_API_BASE_URL /
// VITE_WS_BASE_URL override that for a split dev setup (vite dev server
// on 5173 against the app on 9300).
const origin = (): string =>
  typeof window !== 'undefined' && window.location ? window.location.origin : ''

const getApiBaseUrl = (): string => {
  if (import.meta.env.VITE_API_BASE_URL) return import.meta.env.VITE_API_BASE_URL
  if (import.meta.env.DEV) {
    const host = typeof window !== 'undefined' && window.location ? window.location.hostname : 'localhost'
    return `http://${host}:9300`
  }
  return origin()
}

const getWebSocketUrl = (): string => {
  if (import.meta.env.VITE_WS_BASE_URL) return import.meta.env.VITE_WS_BASE_URL
  const api = getApiBaseUrl() || origin()
  return api.replace(/^https:/, 'wss:').replace(/^http:/, 'ws:')
}

export const config = {
  API_BASE_URL: getApiBaseUrl(),
  WS_BASE_URL: getWebSocketUrl(),
  API_TIMEOUT: 20000,
  UNDER_MAINTENANCE: (() => {
    const raw = import.meta.env.VITE_UNDER_MAINTENANCE
    if (typeof raw === 'string') {
      const normalized = raw.trim().toLowerCase()
      return normalized === 'true' || normalized === '1' || normalized === 'yes' || normalized === 'y'
    }
    return false
  })(),
} as const

export default config
