# App listing screenshots

Screenshots shown on an App Catalog listing. Referenced from
`server/config/apps_index.yml`:

```yaml
- id: license-plate-recognition
  screenshots:
    - app-screenshots/license-plate-recognition/reads.webp
    - app-screenshots/license-plate-recognition/register.webp
```

## Rules (enforced by `scripts/validate_apps_index.py`)

- **Local files only.** `app-screenshots/<app-id>/<file>.(png|jpg|jpeg|webp|avif)`.
  A remote URL is refused: it would leak every catalog viewer's IP to a
  third-party host, and it would not load at all on an air-gapped site.
  Both are things OpenNVR promises against elsewhere — the egress proxy
  exists so an *app* cannot phone home; the catalog should not do it either.
- **The file must exist.** A listing advertising a broken image is worse
  than one with no image, so a missing file fails the CI gate.
- Vite copies `app/public/` into the build, and core serves it from its own
  origin, so no new endpoint and no network round-trip outside the site.

## Guidance

- Show the app doing its job — the surface an operator will actually use.
- Landscape, roughly 16:10, at least 1200px wide. `.webp` keeps the image
  small; these ship inside the frontend bundle.
- No real plates, faces, or customer sites. Use the fake-camera stack
  (`docs/FAKE_CAMERAS.md`) or redact.
