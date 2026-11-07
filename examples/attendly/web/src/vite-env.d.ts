/// <reference types="vite/client" />

interface ImportMetaEnv {
  // API Configuration
  readonly VITE_API_BASE_URL?: string
  readonly VITE_WS_BASE_URL?: string
  
  // Domain Configuration
  readonly VITE_PRODUCTION_DOMAIN?: string
  readonly VITE_DEV_API_URL?: string
  
  // Vite Built-in Variables
  readonly MODE: string
  readonly BASE_URL: string
  readonly PROD: boolean
  readonly DEV: boolean
  readonly SSR: boolean
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}