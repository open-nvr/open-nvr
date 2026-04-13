## OpenNVR UI

React + Vite + Tailwind CSS UI for OpenNVR.

### Development

1) Install deps and run dev server

```bash
npm install
npm run dev
```

2) API base URL

The UI automatically resolves the API base URL:

1. **`VITE_API_BASE_URL`** from `app/.env` (optional override)
2. **Production build**: Uses `window.location.origin` (same origin as frontend)
3. **Development mode**: Uses Vite proxy (automatically routes `/api/*` to backend)

**No configuration needed for standard setups!**

The backend runs on `localhost:8000` by default, and Vite proxy handles routing automatically.

**Optional:** Create `app/.env` only if backend runs on non-standard port:

```env
VITE_API_BASE_URL=http://localhost:9000
```

See `env.example` for details.

### Authentication

The UI requires sign-in. Visit `/login` to authenticate. JWT tokens are stored in localStorage and sent as `Authorization: Bearer <token>`.

Default admin after server init: `admin` / `admin123` (change in production).

### Styling

Tailwind CSS v4 is imported via `src/index.css`.
