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
      // Registered manually after window 'load' (see main.tsx) instead of a
      // parser-blocking <script src="/registerSW.js"> injected into <head>.
      injectRegister: null,
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
        // Precache ONLY the app shell (index.html + icons). The old
        // '**/*.{js,css,...}' pattern precached every route chunk (~2MB)
        // on first load, competing for bandwidth with the requests the
        // current page was actually waiting on. Hashed /assets/* files are
        // immutable, so they runtime-cache on first use instead.
        globPatterns: ['index.html', '*.{ico,svg}', 'pwa-*.png', 'maskable-icon-*.png'],
        maximumFileSizeToCacheInBytes: 5 * 1024 * 1024,
        navigateFallback: '/index.html',
        runtimeCaching: [
          {
            urlPattern: /\/assets\/.*\.(?:js|css|woff2?)$/,
            handler: 'CacheFirst',
            options: {
              cacheName: 'assets-v1',
              expiration: { maxEntries: 200, maxAgeSeconds: 30 * 24 * 3600 },
            },
          },
        ],
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

      // Streaming paths. In production nginx serves the SPA and proxies these
      // to MediaMTX (see nginx/opennvr.conf); the UI relies on that by
      // rebasing every backend-supplied stream URL onto its own origin
      // (src/lib/streamUrl.ts). Under `npm run dev` that origin is this Vite
      // server, so without these entries a WHEP POST hits Vite itself and
      // 404s — live view stays dark. The prefix strips below mirror the
      // `rewrite` directives in the matching nginx locations.
      //
      // Defaults target the Docker stack, which publishes MediaMTX on
      // loopback and serves HLS/WebRTC over HTTPS with a self-signed cert
      // (hlsEncryption/webrtcEncryption: yes in mediamtx.docker.yml) — hence
      // secure:false. A bare-metal MediaMTX (mediamtx.local.yml) has both set
      // to `no`, so point the VITE_MEDIAMTX_* vars at http:// URLs there.
      '/webrtc': {
        target: process.env.VITE_MEDIAMTX_WEBRTC_URL || 'https://127.0.0.1:8889',
        changeOrigin: true,
        secure: false,
        rewrite: (path) => path.replace(/^\/webrtc/, ''),
      },
      '/hls': {
        target: process.env.VITE_MEDIAMTX_HLS_URL || 'https://127.0.0.1:8888',
        changeOrigin: true,
        secure: false,
        rewrite: (path) => path.replace(/^\/hls/, ''),
      },

      // Recording playback. Only MediaMTX's two real endpoints are proxied,
      // matching nginx's exact-match locations, so SPA routes like /playback
      // and /playback/sync still load the UI instead of 404ing here.
      '/playback/get': {
        target: process.env.VITE_MEDIAMTX_PLAYBACK_URL || 'http://127.0.0.1:9996',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/playback/, ''),
      },
      '/playback/list': {
        target: process.env.VITE_MEDIAMTX_PLAYBACK_URL || 'http://127.0.0.1:9996',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/playback/, ''),
      },
    },
  },
})

