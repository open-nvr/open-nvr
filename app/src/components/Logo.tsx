// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The OpenNVR wordmark, drawn inline rather than as <img src=".svg">.
// The file had "Open" filled pure white, and an <img> cannot see the
// app's theme — so on the light theme's grey top bar the word all but
// vanished. Inline, "Open" takes the current text colour and follows
// the theme; "NVR" keeps the brand blue.
export function Logo({ className, style }: { className?: string; style?: React.CSSProperties }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 200 60"
      role="img"
      aria-label="OpenNVR"
      className={className}
      // Inline SVG text inherits CSS text-transform, and the top bar is
      // `uppercase` — without this the wordmark renders as OPENNVR.
      style={{ textTransform: 'none', letterSpacing: 'normal', ...style }}
    >
      <text x="20" y="40" fontFamily="Arial, sans-serif" fontWeight="bold" fontSize="28" fill="currentColor">
        Open<tspan fill="#3b8fe0">NVR</tspan>
      </text>
    </svg>
  )
}
