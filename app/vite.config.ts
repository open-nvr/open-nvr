/**
 * Copyright (c) 2026 OpenNVR
 * This file is part of OpenNVR.
 * 
 * OpenNVR is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 * 
 * OpenNVR is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 * 
 * You should have received a copy of the GNU Affero General Public License
 * along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.
 */

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      injectRegister: 'auto',
      includeAssets: ['opennvr-icon.svg','opennvr-logo.svg'],
      manifest: {
        name: 'OpenNVR',
        short_name: 'OpenNVR',
        description: 'OpenNVR network video recorder UI',
  theme_color: '#1e3a5f',
  background_color: '#0b1220',
        display: 'standalone',
        start_url: '/',
        scope: '/',
        icons: [
          { src: 'pwa-64x64.png', sizes: '64x64', type: 'image/png' },
          { src: 'pwa-192x192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
          { src: 'maskable-icon-512x512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg}'],
        // Increase default 2MiB limit to allow larger assets
        maximumFileSizeToCacheInBytes: 5 * 1024 * 1024,
        navigateFallback: '/index.html',
      },
      devOptions: {
        enabled: true,
      },
      // Generate and inject icons and head links from a single source image
      pwaAssets: {
        image: 'public/opennvr-icon.svg',
        preset: 'minimal-2023',
        htmlPreset: '2023',
        overrideManifestIcons: true,
      },
    }),
  ],
  server: {
    proxy: {
      '/api': {
        // Backend API proxy - can be overridden via VITE_API_BASE_URL env var
        target: process.env.VITE_API_BASE_URL || 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, '/api'),
      },
    },
  },
})

