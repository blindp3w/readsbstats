# Contributing to readsbstats

Thanks for your interest in contributing! This guide covers what you need to get started.

## Setup

```bash
git clone https://github.com/YOUR_USER/readsbstats.git
cd readsbstats
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

For local development with real data, copy the database from your Pi:

```bash
rsync pi@your-pi:/mnt/ext/readsbstats/history.db ./db/
bash dev.sh
```

## Running tests

```bash
.venv/bin/pytest
```

All tests use in-memory SQLite — no Pi or external services needed.

For coverage:

```bash
.venv/bin/pytest --cov=. --cov-report=term-missing --ignore=.venv
```

Target: maintain the current ~99% coverage. New features and bug fixes should include tests.

## Code style

**Python:**
- PEP 8, snake_case naming
- Double quotes for strings
- Type hints on function signatures
- Keep imports ordered: stdlib, third-party, local

**JavaScript:**
- `const` / `let` (no `var`)
- camelCase for variables and functions, UPPER_SNAKE for constants
- Use `addEventListener()` for event binding (not inline `onclick` or property assignment)
- Escape API data with `escHtml()` before inserting into innerHTML

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
scripts/           # CLI one-shot tools + deployment shell scripts
tests/             # pytest test suite (Python) + node --test JS suite
templates/         # Jinja2 HTML templates
static/            # CSS, JS, vendor assets (Leaflet, uPlot), airspace GeoJSON
systemd/           # .service / .timer unit files
docs/              # User-facing docs
```

The collector (`collector.py`) runs as a systemd service polling `/run/readsb/aircraft.json`
every 5 s and writing to SQLite. The web server (`web.py`) is a FastAPI + Uvicorn app bound
to `127.0.0.1:8080`, fronted by nginx at `/stats/`. Database schema and migrations live in
`database.py`; all tunables go through `RSBS_*` env vars in `config.py`.
