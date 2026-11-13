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

import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig } from 'eslint/config'
import tseslint from 'typescript-eslint'

// Every rule in the TypeScript block starts life as a warning. The .ts/.tsx
// source was never linted before, so turning these on as errors would fail
// CI on day one; warnings make the debt visible without blocking anyone.
// Rules get promoted to errors one at a time as the code is cleaned up.
const asWarnings = (configs) =>
  configs.map((c) => ({
    ...c,
    rules: Object.fromEntries(
      Object.entries(c.rules ?? {}).map(([name, value]) => [
        name,
        Array.isArray(value)
          ? (value[0] === 'off' || value[0] === 0 ? value : ['warn', ...value.slice(1)])
          : (value === 'off' || value === 0 ? value : 'warn'),
      ]),
    ),
  }))

const NATIVE_DIALOGS = ['confirm', 'alert', 'prompt']
const NATIVE_DIALOG_MESSAGE =
  'Use useConfirm() from components/ui/ConfirmDialog, useSnackbar(), or an inline field instead of a browser dialog.'
const OVERLAY = '/\\bfixed inset-0\\b/'
const OVERLAY_MESSAGE = 'Use <Modal> from components/Modal instead of a hand-built overlay.'
const PALETTE =
  '/\\b(bg|text|border|ring|divide|outline|fill|stroke|from|to|via|accent|placeholder)-(slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\\d{2,3}\\b/'
const PALETTE_MESSAGE =
  'Use a theme token (bg-[var(--panel)], text-[var(--danger)], ...) so the light theme works; see docs/UI_GUIDELINES.md.'

export default defineConfig([
  { ignores: ['dist', 'dev-dist'] },
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs['recommended-latest'],
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    rules: {
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]' }],
    },
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    extends: asWarnings([
      ...tseslint.configs.recommended,
      reactHooks.configs['recommended-latest'],
      reactRefresh.configs.vite,
    ]),
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
  },
  {
    // Promoted: zero violations when TS linting was switched on, and a hook
    // called conditionally is a crash waiting for the branch that skips it.
    files: ['src/**/*.{ts,tsx}'],
    rules: { 'react-hooks/rules-of-hooks': 'error' },
  },
  {
    // The UI conventions in docs/UI_GUIDELINES.md, enforced. Each of these
    // reached zero in the GUI refactor; these errors keep them there.
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-globals': ['error', ...NATIVE_DIALOGS.map((name) => ({ name, message: NATIVE_DIALOG_MESSAGE }))],
      'no-restricted-properties': ['error', ...NATIVE_DIALOGS.map((property) => ({ object: 'window', property, message: NATIVE_DIALOG_MESSAGE }))],
      'no-restricted-syntax': ['error',
        { selector: `Literal[value=${OVERLAY}]`, message: OVERLAY_MESSAGE },
        { selector: `TemplateElement[value.raw=${OVERLAY}]`, message: OVERLAY_MESSAGE },
        { selector: `Literal[value=${PALETTE}]`, message: PALETTE_MESSAGE },
        { selector: `TemplateElement[value.raw=${PALETTE}]`, message: PALETTE_MESSAGE },
      ],
    },
  },
  {
    // Layers that are not dialogs, or must not follow the theme: the shared
    // Modal itself, full-screen video and camera views, the device-blocked
    // screen, the mobile nav drawer, stacked pickers and paper print sheets.
    files: [
      'src/components/Modal.tsx',
      'src/components/ui/**',
      'src/components/DeviceBlockedOverlay.tsx',
      'src/components/EvidenceViewer.tsx',
      'src/components/PlaybackConsole.tsx',
      'src/components/PrintSheet.tsx',
      'src/components/QrScanner.tsx',
      'src/components/RecordingBrowser.tsx',
      'src/components/VideoPlayer/**',
      'src/shell/AppShell.tsx',
      'src/views/apps/StackedDialog.tsx',
      'src/views/PlaybackView.tsx',
      'src/views/guardscan/ScreeningReport.tsx',
    ],
    rules: { 'no-restricted-syntax': 'off' },
  },
])

