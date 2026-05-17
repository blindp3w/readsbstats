# nginx serves SPA static assets directly (bypassing FastAPI)

- Status: ACCEPTED
- Date: 2026-05-17

## Context

The React SPA produces hashed asset bundles under `frontend/dist/assets/` (e.g., `index-Abc123.js`). Routing them through FastAPI meant every JS/CSS request hit Python's uvicorn worker and its GIL, adding unnecessary latency on the Pi.

## Decision

nginx serves `frontend/dist/assets/` and `frontend/dist/favicon.svg` directly via `alias` directives with `expires 1y; Cache-Control: public, immutable` — bypassing FastAPI entirely for static assets. This requires nginx's runtime user (`www-data` / `nginx`) to be in the `readsbstats` group.

FastAPI keeps `/assets` mount and `/favicon.svg` route as **fallbacks** for direct `:8080` access (dev mode, tests) — they are not load-bearing in production.

`scripts/update.sh` loops over both `www-data` and `nginx` user candidates and adds whichever exist to the `readsbstats` group, then restarts nginx on first add (group membership only applies at process start, not reload).

## Consequences

- Static asset latency drops to nginx's native file serving speed.
- Deployed `index.html` is `Cache-Control: no-store` (the hashed asset URLs inside change every deploy).
- The catch-all `/{spa_path:path}` in FastAPI 404s any path ending in a known asset extension (`.js`, `.css`, `.svg`, …) instead of returning HTML — so a missing/stale Vite build surfaces as a real 404 rather than the SPA shell masquerading as JS.
