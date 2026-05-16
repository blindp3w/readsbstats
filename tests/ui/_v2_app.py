"""Test-only ASGI wrapper that strips /stats/ from incoming request paths.

In production, nginx terminates the /stats/ subpath and proxies the bare
path to uvicorn (which has --root-path /stats set as metadata). The
Playwright test fixture has no nginx — so we wrap the app with a small
ASGI middleware that does the same prefix strip. This lets the SPA dist's
baked-in asset URLs (/stats/assets/*.js after the v2.0.0 cutover) resolve
correctly against the test server.

Only used by the Playwright fixture in tests/ui/conftest.py. Not part of
the production code path.
"""
from readsbstats.web import app as _app

_PREFIX = "/stats"


async def app(scope, receive, send):  # type: ignore[no-untyped-def]
    if scope["type"] in {"http", "websocket"}:
        path = scope.get("path", "")
        raw_path = scope.get("raw_path") or path.encode("latin-1")
        if path == _PREFIX:
            new_path = "/"
            new_raw = b"/"
        elif path.startswith(_PREFIX + "/"):
            new_path = path[len(_PREFIX):]
            new_raw = raw_path.replace(_PREFIX.encode("latin-1"), b"", 1)
        else:
            new_path = path
            new_raw = raw_path
        # Don't set root_path here — Starlette mounts re-apply root_path
        # during match, so leaving it empty after the strip is what makes
        # /assets resolve. FastAPI was already constructed with root_path
        # metadata via the RSBS_ROOT_PATH env var.
        scope = {**scope, "path": new_path, "raw_path": new_raw, "root_path": ""}
    return await _app(scope, receive, send)
