// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// `npm run dev` serves the page on :5174 and sends /api and /auth to a
// running attendance app (ATTENDANCE_WEB_URL, default the e2e port), so
// the page can be worked on against a real core.
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const target = process.env.ATTENDANCE_WEB_URL ?? 'https://localhost:9218'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/api': { target, changeOrigin: true, secure: false },
      '/auth': { target, changeOrigin: true, secure: false },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: false },
})
