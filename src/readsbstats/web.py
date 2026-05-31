"""
readsbstats — FastAPI web server.

Serves the flight history web UI and JSON API.
Run via uvicorn (see systemd/readsbstats-web.service).

This module is the app factory + lifespan + router-include point. Domain
endpoints live in ``api/*.py``; cache state lives in ``cache``; SPA-shell
routes (``/``, ``/favicon.svg``, ``/v2[/{rest}]``, ``/live``,
``/{spa_path:path}``) stay registered on ``app`` here so their order is
unambiguous (catch-all must come LAST).
"""

import asyncio
import logging
import sys
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import analytics, cache, config, database, route_enricher
from .api import _deps  # noqa: F401  — re-exposed below for back-compat
from .api import (
    aircraft as _api_aircraft,
    airspace as _api_airspace,
    dates as _api_dates,
    feeders as _api_feeders,
    flights as _api_flights,
    health as _api_health,
    map as _api_map,
    settings as _api_settings,
    stats as _api_stats,
    watchlist as _api_watchlist,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("web")

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _startup_migrate() -> None:
    """Bring the DB to a queryable baseline at web startup.

    Audit-13 A13-067: `_migrate` may run a handful of ALTER TABLEs and
    `CREATE INDEX` statements on cold disk — hundreds of ms on a Pi 4 — so the
    lifespan runs this in a worker thread to keep the event loop free.

    BE-3 (Audit 2026-05-31): for a real DB path, go through
    `database.ensure_base_schema()` — it creates base tables on a fresh
    `RSBS_DB_PATH` (so endpoints don't raise `no such table`) and recovers an
    interrupted aircraft_db swap, in addition to `_migrate()`. It opens and
    closes its own connection, so the worker thread leaves no thread-local
    connection open (A14-004).

    When a test has injected ``_deps._db``, migrate it in place — in-memory
    DBs can't be reopened by path, and the test owns the connection.
    """
    if _deps._db is not None:
        database._migrate(_deps._db)
        return
    database.ensure_base_schema()


@asynccontextmanager
async def _lifespan(app_: FastAPI) -> AsyncIterator[None]:
    log.info("Starting web server — DB: %s", config.DB_PATH)
    await asyncio.to_thread(_startup_migrate)
    # Background migrations (positions indexes, bearing backfill) are owned by
    # the collector so two processes don't fight on the SQLite write lock.
    route_enricher.start_background_enricher()
    # Eager-init DuckDB so the first user hit doesn't pay extension+ATTACH
    # cost (~1–2 s). If the engine is up, also kick off the prewarmer so
    # users land on warm cache instead of triggering the cold-scan path.
    if analytics.is_available():
        log.info("analytics: DuckDB engine ready")
        if config.PREWARM_MAP_CACHE:
            log.info("starting map-cache prewarmer (8 targets, half-TTL refresh)")
            cache._start_prewarmer()
    yield
    cache._stop_prewarmer()
    analytics.close()
    log.info("Web server stopped")


app = FastAPI(root_path=config.ROOT_PATH, docs_url=None, redoc_url=None, lifespan=_lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
# `/static` still serves /api/airspace's bundled GeoJSON and the favicon
# fallback. The old Jinja JS/CSS subtrees were removed at v2.0.0 cutover.
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# ---------------------------------------------------------------------------
# React SPA mount.  Serves the Vite build from frontend/dist/ at the root of
# the nginx prefix (/stats/ externally; / internally because of root_path).
# Gated by presence of the built artefacts so a missing dist (e.g. fresh
# clone, mid-rsync) doesn't crash the worker — the API surface keeps working
# but every UI path returns 404.
#
# index.html is served per-request (not cached at import) so atomic-swap
# deploys take effect without restart; assets are mounted via StaticFiles and
# can be long-cached because their URLs are content-hashed.
#
# /v2/* paths from the v2.0.0-rc.1 era 301-redirect to / so RC bookmarks
# keep working.
# ---------------------------------------------------------------------------
SPA_DIR = BASE_DIR / "frontend" / "dist"
SPA_ASSETS = SPA_DIR / "assets"
SPA_INDEX = SPA_DIR / "index.html"

_SPA_AVAILABLE = SPA_INDEX.is_file() and SPA_ASSETS.is_dir()

if _SPA_AVAILABLE:
    app.mount("/assets", StaticFiles(directory=SPA_ASSETS), name="spa-assets")

    # Top-level static files emitted by Vite from `frontend/public/` — they
    # land at the root of `dist/`, NOT under `dist/assets/`, so the /assets
    # mount above doesn't catch them. Add explicit routes for each one
    # (rather than a StaticFiles mount at "/" which would shadow /api/*).
    from fastapi.responses import FileResponse  # local import to keep top of file tidy

    @app.get("/favicon.svg", include_in_schema=False)
    def _favicon_svg() -> FileResponse:
        return FileResponse(
            SPA_DIR / "favicon.svg",
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )


# Audit-13 A13-005: /v2 compat redirect lives OUTSIDE the SPA-availability
# gate. During a mid-rsync deploy the SPA dist may be briefly absent;
# /v2 bookmarks should still rewrite their URL bar to the canonical
# scheme even though the target path itself will 404 until the deploy
# completes. Old `if _SPA_AVAILABLE:`-gated registration meant /v2/...
# 404'd outright during the same window.
@app.get("/v2", include_in_schema=False)
@app.get("/v2/{rest:path}", include_in_schema=False)
def _v2_compat(request: Request, rest: str = "") -> RedirectResponse:
    root = request.scope.get("root_path", "").rstrip("/")
    sanitized = _sanitize_v2_rest(rest)
    target = f"{root}/{sanitized}" if sanitized else f"{root}/"
    # CodeQL #29 — explicit recognized sanitizer pattern. `_sanitize_v2_rest`
    # already strips the leading `/` and `\` that produce scheme-relative
    # URLs, but CodeQL's data-flow analysis doesn't know our custom helper
    # is safe (CWE-601 / `py/url-redirection`). This urlparse() check is
    # the pattern CodeQL's own documentation recommends, and serves as a
    # defence-in-depth catch: a safe redirect target has neither scheme
    # nor netloc. If anything slips through the sanitizer, fall back to
    # the SPA root rather than honour the redirect.
    parsed_target = urllib.parse.urlparse(target)
    if parsed_target.scheme or parsed_target.netloc:
        return RedirectResponse(url=f"{root}/", status_code=301)
    return RedirectResponse(url=target, status_code=301)


def _sanitize_v2_rest(rest: str) -> str:
    """Return a safe path suffix for the /v2 → / redirect.

    Hardening against:
      * Open-redirect (CodeQL #28): a crafted `/v2//evil.com` would otherwise
        produce a Location starting with `//` (browsers treat that as
        scheme-relative and follow off-site). We strip leading `/` and `\\`
        characters — some browsers treat the latter as the former in URLs.
      * Response splitting (audit-12 #149): Starlette rejects raw CR/LF in
        path parameters today, but if a future ASGI server change weakens
        that we don't want CR/LF reaching the Location header.
      * Header validity (audit-12 P8 follow-up): percent-encode the remaining
        path so spaces / quotes / other URL-special characters can't
        produce a malformed Location. The original ``_sanitize_v2_rest``
        landed only the strip; the quote step was always part of the
        audit's recommended fix.
    """
    rest = rest.lstrip("/\\")
    rest = rest.replace("\r", "").replace("\n", "")
    return urllib.parse.quote(rest, safe="/")


# ---------------------------------------------------------------------------
# /live — historical alias for /map (not a real SPA page)
# ---------------------------------------------------------------------------
@app.get("/live", include_in_schema=False)
def redirect_live(request: Request) -> RedirectResponse:
    # Audit-13 A13-049: defence in depth against a hostile reverse-proxy
    # injecting an absolute root_path. Use the same urlparse() check
    # _v2_compat uses (which CodeQL recognises as a recognised sanitiser).
    root = request.scope.get("root_path", "").rstrip("/")
    target = f"{root}/map"
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme or parsed.netloc:
        return RedirectResponse(url="/map", status_code=302)
    return RedirectResponse(url=target, status_code=302)


# ---------------------------------------------------------------------------
# Domain routers — register BEFORE the SPA catch-all
# ---------------------------------------------------------------------------
# Order within this block doesn't matter; every path begins with /api/...
app.include_router(_api_settings.router)
app.include_router(_api_watchlist.router)
app.include_router(_api_flights.router)
app.include_router(_api_aircraft.router)
app.include_router(_api_stats.router)
app.include_router(_api_airspace.router)
app.include_router(_api_map.router)
app.include_router(_api_dates.router)
app.include_router(_api_health.router)
app.include_router(_api_feeders.router)


# ---------------------------------------------------------------------------
# SPA root catch-all — MUST be registered last so it doesn't shadow literal
# /api/* routes, the compat redirects, or /static. FastAPI's router tries
# routes in registration order; this `path:path` parameter matches anything,
# so it's the final fallback.
# ---------------------------------------------------------------------------

if _SPA_AVAILABLE:
    _SPA_ASSET_EXTS = {
        ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".svg",
        ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf",
        ".map", ".json", ".txt",
    }

    @app.get("/", include_in_schema=False)
    @app.get("/{spa_path:path}", include_in_schema=False)
    def _spa(spa_path: str = "") -> Response:
        # Surface missing-asset 404s instead of returning the SPA shell —
        # masking them as HTML hides deploy mistakes (blank page in browser
        # tries to execute HTML as JS/CSS).
        last = spa_path.rsplit("/", 1)[-1]
        if "." in last:
            ext = "." + last.rsplit(".", 1)[-1].lower()
            if ext in _SPA_ASSET_EXTS:
                raise HTTPException(status_code=404)
        try:
            body = SPA_INDEX.read_bytes()
        except FileNotFoundError:  # pragma: no cover — disappears mid-flight
            raise HTTPException(status_code=503, detail="SPA dist missing")
        return Response(
            content=body,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )
