# Contributing to readsbstats

Thanks for your interest in contributing! This guide covers what you need to get started.

## Setup

```bash
git clone https://github.com/YOUR_USER/readsbstats.git
cd readsbstats
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .        # makes `from readsbstats import ...` resolve in tests
```

For local development with real data, copy the database from your Pi:

```bash
mkdir -p db
# 1. Snapshot on the Pi into RAM-backed /dev/shm (never /tmp: it is on the SD card).
#    One read transaction, safe against the live WAL database. Check space first.
#    The path is your RSBS_DATA_DIR/history.db (default /var/lib/readsbstats).
echo "VACUUM INTO '/dev/shm/history.db'" | ssh pi@your-pi \
  'df -h /dev/shm; nice -n19 ionice -c3 sqlite3 "file:/var/lib/readsbstats/history.db?mode=ro"'
# 2. Pull it, then delete it on the Pi.
rsync pi@your-pi:/dev/shm/history.db ./db/
ssh pi@your-pi 'rm /dev/shm/history.db'
# 3. Verify.
sqlite3 db/history.db "PRAGMA integrity_check"
bash scripts/dev.sh
```

The database file is owned by `readsbstats` but world-readable, so your SSH user
can run the `sqlite3` command itself. Never run `sqlite3 .backup` against the
live database — it restarts on every collector write and never finishes. If you
copy the raw files instead, copy `history.db` together with `history.db-wal`,
delete the copied `-shm`, then run `integrity_check`.

### Frontend

```bash
cd frontend && npm install
npm run dev   # HMR dev server at :5173, proxies /api → :8080
npm test      # Vitest unit tests
```

## Running tests

```bash
.venv/bin/pytest
```

All tests use in-memory SQLite — no Pi or external services needed.

For coverage:

```bash
.venv/bin/pytest --cov=readsbstats --cov-report=term-missing
```

Target: keep the current ~98% statement coverage; CI fails below 93%. New features and bug fixes should include tests.

UI tests (Playwright, WebKit + Chromium device matrix) live in `tests/ui/` and are
excluded from the default run — see [docs/development.md](docs/development.md) for
setup. CI runs the full suite on every PR (`pytest -m ui tests/ui`), so run it
locally before pushing UI changes.

## Dependency updates (Dependabot)

Dependabot opens grouped PRs weekly (one each for npm, pip and GitHub Actions)
plus a separate, ungrouped PR per security fix as soon as an alert has a patch
(the `groups` in `.github/dependabot.yml` set no `applies-to`, so they cover
version updates only). Before merging:

- CI must be green — it runs the backend suite, frontend build + lint + Vitest,
  and the full Playwright UI suite.
- Read the release notes of every **major** bump in the group; a failing
  `npm ci` (`ERESOLVE`) usually means one package's peer range lags another.
- Map-related bumps (`maplibre-gl`, `react-map-gl`): unit tests mock the map,
  so check the live map + a flight route map render after deploy.
- TypeScript majors are ignored in `.github/dependabot.yml` until
  typescript-eslint supports them; drop the rule when it does.

## Code style

**Python:**
- PEP 8, snake_case naming
- Double quotes for strings
- Type hints on function signatures
- Keep imports ordered: stdlib, third-party, local

**TypeScript / React** (`frontend/src/`):
- React 19 function components with hooks — no class components
- `const` / `let` (no `var`); camelCase for variables and functions, PascalCase for components, UPPER_SNAKE for constants
- Type props and API payloads explicitly; avoid `any`
- Route any third-party URL or `href` through `lib/safeUrl.ts`; avoid `dangerouslySetInnerHTML`
- `npm run lint` must pass with zero warnings (CI runs `eslint . --max-warnings 0`)

**SQL:**
- Use parameterized queries (`?` placeholders) — never interpolate user input
- Use `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`
- New columns on existing tables go through `_migrate()` in `database.py`

**Tests:**
- Use `@pytest.fixture(autouse=True)` for per-test setup within test classes
- Use the `make_db()` helper pattern for in-memory SQLite
- Bug fixes: write a failing test first, then fix the code

## Pull requests

1. Fork and create a feature branch
2. Make your changes with tests
3. Run the full test suite
4. Open a PR with a clear description of what and why

Keep PRs focused — one feature or fix per PR.

## Project structure

```
src/readsbstats/   # Python package — collector, web server, enrichment, notifier
frontend/          # React 19 + Vite 8 SPA (npm run dev / npm run build)
scripts/           # CLI one-shot tools + deployment shell scripts
tests/             # pytest test suite (Python) + Playwright UI tests
systemd/           # .service / .timer unit files
docs/              # User-facing docs
```

The collector (`collector.py`) runs as a systemd service polling `/run/readsb/aircraft.json`
every 5 s and writing to SQLite. The web server (`web.py`) is a FastAPI + Uvicorn app bound
to `127.0.0.1:8080`, fronted by nginx at `/stats/`, serving a React 19 SPA built with Vite.
Database schema and migrations live in `database.py`; all tunables go through `RSBS_*` env
vars in `config.py`.
