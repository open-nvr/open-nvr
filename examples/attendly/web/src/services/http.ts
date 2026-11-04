// Every request the pages make — through the shared `api` instances or the
// bare `axios` default — carries the OpenNVR bearer, and a 401 sends the
// user back to the login card. Imported once from main.tsx.
import axios from 'axios'

const TOKEN_KEY = 'access_token'

export function attachAuth(instance: typeof axios | ReturnType<typeof axios.create>) {
  instance.interceptors.request.use((config) => {
    const token = localStorage.getItem(TOKEN_KEY)
    if (token) {
      config.headers = config.headers ?? {}
      ;(config.headers as any).Authorization = `Bearer ${token}`
    }
    return config
  })
  instance.interceptors.response.use(
    (response) => response,
    (error) => {
      if (error.response?.status === 401 && !String(error.config?.url || '').includes('/api/auth/login')) {
        localStorage.removeItem(TOKEN_KEY)
        localStorage.removeItem('refresh_token')
        localStorage.removeItem('user')
        if (window.location.pathname !== '/login') window.location.href = '/login'
      }
      return Promise.reject(error)
    },
  )
}

attachAuth(axios)
