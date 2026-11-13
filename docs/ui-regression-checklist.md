# UI regression checklist

This is a manual pass for UI refactors. The automated net catches a lot:
- route smoke and popup smoke tests
- admin and settings flows
- the before/after sweep with its API contract

This list covers what the automated net can't reach: real video, hardware, printing, and judgement calls. Tick every item for each page a change touches, in both **dark and light** themes. Where a line says "(FR)", switch the language to French too.

Run it against a local stack with fake cameras and at least two cameras publishing.

Legend:
- `[auto]` means an automated test already covers it. Glance at it anyway.
- Everything else is manual.

## Shell

- [ ] Sidebar:
  - [ ] every group expands; the active page is highlighted
  - [ ] collapse/expand works
  - [ ] an app route opens its group
- [ ] Top bar:
  - [ ] the clock ticks
  - [ ] Live View shortcut works
  - [ ] fullscreen toggles
  - [ ] theme toggles and the choice persists across reload
  - [ ] EN/FR switch works (FR)
- [ ] Alarm bell:
  - [ ] badge count is shown
  - [ ] panel opens `[auto]`
  - [ ] acknowledge one alarm
  - [ ] acknowledge all
  - [ ] the siren or ping sounds for a new critical alarm
- [ ] Account menu:
  - [ ] opens `[auto]`
  - [ ] Settings link works
  - [ ] Sign out returns to login
- [ ] The disk-critical banner appears when the disk is low and can be dismissed.
- [ ] A crash inside one page shows the error card and the sidebar still works.

## Monitoring

- [ ] **Dashboard:**
  - [ ] health bar chips are correct
  - [ ] vitals tiles link to the right pages
  - [ ] live wall plays real video
  - [ ] live wall tours, pauses on hover, spotlights a camera, and its 1/4/6/9 buttons work
  - [ ] Customise: drag, resize, swap, add, remove, reset, and the layout persists `[auto: add menu]`
- [ ] **Live View:**
  - [ ] each grid layout (1×1 to 4×4 and custom) shows video
  - [ ] drag a camera onto a tile
  - [ ] Fill/Fit
  - [ ] Boxes overlay
  - [ ] Fullscreen
  - [ ] PTZ pad moves a PTZ camera
  - [ ] snapshot downloads
  - [ ] WebRTC ↔ HLS switch
  - [ ] Menu tiles each go somewhere real
- [ ] **Recordings (`/playback/sync`; `/playback` redirects here):**
  - [ ] calendar marks days with footage
  - [ ] multi-camera timeline plays in sync `[auto]`
  - [ ] scrub, speed and step controls work
  - [ ] Export: a dragged range downloads one MP4 per ticked camera
  - [ ] upload a camera's day to cloud works, with a confirmation (superuser)
- [ ] **Search:**
  - [ ] a natural-language query returns results
  - [ ] table/grid toggle
  - [ ] pagination
  - [ ] the Follow/journey panel opens
- [ ] **Cameras:**
  - [ ] list `[auto]`
  - [ ] add: manual, discover and QR `[auto: manual]`
  - [ ] edit
  - [ ] delete `[auto]`
  - [ ] bulk delete and bulk assign
  - [ ] search filters `[auto]`
  - [ ] paging
- [ ] **Alarms / Alerts & Incidents:**
  - [ ] Alarms, Network IDS and System tabs
  - [ ] filters
  - [ ] acknowledge `[auto]`
  - [ ] evidence images open in the viewer
  - [ ] sound policy
  - [ ] test alarm
  - [ ] site arming mode

## Applications (with the app installed and a camera assigned)

- [ ] **App Catalog:**
  - [ ] installed and available lists
  - [ ] install flow
  - [ ] configure dialog, including camera picker and zone editor
  - [ ] licence forget, with a confirmation
- [ ] **App view:**
  - [ ] enable/disable toggle, with a confirmation on disable
  - [ ] actions
  - [ ] live state
- [ ] **Vehicles:**
  - [ ] plate list `[auto]`
  - [ ] registry add/edit/delete
  - [ ] monitoring
  - [ ] report prints
  - [ ] every tab
- [ ] **Occupancy:**
  - [ ] counts
  - [ ] heatmap dialog
  - [ ] report prints
- [ ] **People:**
  - [ ] enrol a face
  - [ ] edit
  - [ ] remove, with a confirmation
  - [ ] stranger dialog
- [ ] **Tripwires / Loitering / Perimeter / Left items / Deliveries / Gates / Notifications:**
  - [ ] stats
  - [ ] camera card
  - [ ] alarm table
  - [ ] a gate "run" action works
  - [ ] notifier backtest
- [ ] **Guard compliance:**
  - [ ] report
  - [ ] evidence viewer
  - [ ] print

## AI & detections

- [ ] **AI Models (BYOM):**
  - [ ] add, edit and delete a model
  - [ ] credentials
  - [ ] recording browser picks a clip
- [ ] **Detection results:**
  - [ ] filters
  - [ ] limit/paging
  - [ ] camera panel
  - [ ] delete old results, with a confirmation
- [ ] **AI Adapters:**
  - [ ] health
  - [ ] metrics
  - [ ] permission grant
  - [ ] permission revoke, with a confirmation

## Security, network, governance

- [ ] **Network:**
  - [ ] Camera LAN and Uplink fields show the saved values
  - [ ] save works
- [ ] **Audit logs:**
  - [ ] filters
  - [ ] paging
  - [ ] details dialog
- [ ] **Compliance:**
  - [ ] KPIs
  - [ ] coverage
  - [ ] CSV export downloads
- [ ] **Access control:**
  - [ ] users create/delete `[auto]`
  - [ ] users edit and activate (MFA)
  - [ ] roles `[auto]`
  - [ ] permissions save
  - [ ] password policy `[auto]`
- [ ] **BYOK:**
  - [ ] upload a certificate and key
  - [ ] info dialog `[auto: opens]`

## Administration and settings

- [ ] **Media Server (`/updates` and Settings → Media Server Manager):**
  - [ ] form and JSON modes
  - [ ] save
  - [ ] active paths
- [ ] **Integrations:**
  - [ ] add `[auto: opens]`
  - [ ] test connection
  - [ ] configure
  - [ ] delete
- [ ] **Cloud:**
  - [ ] add stream `[auto: opens]`
  - [ ] start/stop
  - [ ] edit
  - [ ] delete
  - [ ] S3 settings
- [ ] **Firmware:**
  - [ ] system info
  - [ ] check updates
  - [ ] apply, with a confirmation
  - [ ] auto-update
- [ ] **Support:**
  - [ ] run diagnostics
  - [ ] MediaMTX snapshot
  - [ ] support bundle downloads
- [ ] **Settings → Camera-Config:**
  - [ ] device settings: every one of the 16 tabs loads
  - [ ] reboot and lock confirmations
  - [ ] streaming table
  - [ ] zones add/delete
- [ ] **Settings → Recording:**
  - [ ] retention `[auto]`
  - [ ] pause setting
  - [ ] orphans
- [ ] **Settings → Deleted cameras:**
  - [ ] list
  - [ ] playback
  - [ ] purge, with a confirmation
- [ ] **Settings → Firewall:**
  - [ ] approve
  - [ ] block and forget, both confirmed
  - [ ] never block your own session by accident
- [ ] **Settings → API tokens:**
  - [ ] create and revoke `[auto]`
- [ ] **Settings → More:**
  - [ ] WebRTC save
  - [ ] Window layouts: create, edit, set as default, delete
  - [ ] Uplink save
  - [ ] System health `[auto]`

## Auth

- [ ] Login:
  - [ ] wrong password shows the attempts left
  - [ ] MFA verify
  - [ ] first-time setup, both steps
  - [ ] forced MFA setup on a new account
  - [ ] sign-out and back links on MFA screens

## Every page

- [ ] Page title is correct.
- [ ] Loading, empty and error states all render (stop core briefly to see the error state).
- [ ] No sideways scroll at 1920, 1366, 820 and 390px widths.
- [ ] Keyboard:
  - [ ] Tab reaches every control
  - [ ] Escape closes popups
  - [ ] focus returns to the button that opened the popup
- [ ] (FR) No English left over and no clipped labels.
