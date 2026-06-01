# Development

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
.venv/bin/pip install -r requirements-dev.txt   # pytest, coverage
```

Copy `history.db` from the Pi for real data locally:

```bash
rsync your-pi.local:/mnt/ext/readsbstats/history.db ./db/
```

## Run the web server

```bash
bash scripts/dev.sh
```

Opens at `http://127.0.0.1:8080/`. `--reload` enabled — saving any `.py` restarts automatically. For SPA hot-reload:

```bash
( cd frontend && npm run dev )   # → http://127.0.0.1:5173/, proxies /api to :8080
```

`RSBS_ROOT_PATH=""` is required locally (no nginx proxy in dev mode).

## Test the collector

```bash
# Terminal 1 — generate a rotating aircraft.json
.venv/bin/python -m readsbstats.sim

# Terminal 2 — run the collector against it
RSBS_AIRCRAFT_JSON=/tmp/rsbs_sim.json RSBS_DB_PATH=./db/history.db \
  .venv/bin/python -m readsbstats.collector
```

## Running tests

```bash
.venv/bin/pytest                                              # 1600+ Python tests
( cd frontend && npm test )                                   # 323 Vitest tests

# Coverage
.venv/bin/pytest --cov=readsbstats --cov-report=term-missing
```

### Playwright mobile UI tests (optional, local only)

```bash
# One-time setup
pip install -e ".[ui-tests]"
playwright install chromium webkit

# Run
pytest tests/ui/ -v -m ui          # 84 tests (React SPA, 6 devices × pages)
pytest tests/ui/ -v -m ui -k "settings"   # single page
```

Screenshots saved to `tests/ui/screenshots/` (gitignored).

| Test file | Coverage |
|---|---|
| `tests/test_web.py` | FastAPI routes, filter helpers, cache, personal records |
| `tests/test_collector.py` | Haversine, flight detection, ghost/GS filters |
| `tests/test_notifier.py` | Telegram notifications, daily summary |
| `tests/test_purge_*.py` | Purge scripts (ghosts, bad GS, MLAT spikes) |
| `tests/test_route_enricher.py` | Route enrichment |
| `tests/test_icao_ranges.py` | ICAO address → country lookup |
| `tests/test_db_updater.py` | Aircraft/airline DB download |
| `tests/test_map.py` | `/map` routes and snapshot API |
| `tests/ui/test_mobile_smoke.py` | Playwright: 84 tests across all pages and viewports |

## Build and deploy to the Pi

```bash
# Build the SPA (required when frontend/src/ changes)
( cd frontend && npm ci && npm run build )

# Sync and restart
rsync -avz --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.venv' --exclude='*.egg-info' \
  --exclude='docs' --exclude='db' --exclude='.DS_Store' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='frontend/node_modules' --exclude='frontend/.vite' --exclude='frontend/coverage' \
  --exclude='internal_docs' --exclude='.claude' --exclude='CLAUDE.md' \
  ~/projects/readsbstats/ your-pi.local:/tmp/readsbstats/
ssh your-pi.local sudo bash /tmp/readsbstats/scripts/update.sh
```

`update.sh` refuses to run if `frontend/dist/` is missing or stale relative to `frontend/src/`. The rsync goes to `dist.new/` then atomic-renames to `dist/` so a half-finished sync never serves a broken page.

## Frontend notes

`frontend/.npmrc` pins `registry=https://registry.npmjs.org/` — never remove this. Without it, a dev machine's `~/.npmrc` pointing at a company registry can bake internal URLs into `package-lock.json`, breaking CI.

The SPA is built with `vite build` and served from `frontend/dist/`. In production nginx serves `frontend/dist/assets/` directly with long-cache headers; FastAPI keeps `/assets` as a fallback for direct `:8080` access.
