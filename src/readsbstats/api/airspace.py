"""Airspace GeoJSON overlay endpoint."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter

from .. import cache, config


log = logging.getLogger("web")
router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent


@router.get("/api/airspace")
def api_airspace() -> dict:
    """Serve the configured airspace GeoJSON (default: bundled poland.geojson)."""
    cached = cache._get_cache("airspace")
    if cached is not None:
        return cached

    path = config.AIRSPACE_GEOJSON or str(_BASE_DIR / "static" / "airspace" / "poland.geojson")
    # improvements.md #73: env-set paths must resolve to a regular file so a
    # misconfiguration can't make us read /dev/random or follow a symlink to
    # an unintended target.  A13-041 size cap stays in place below.
    _AIRSPACE_MAX_BYTES = 10 * 1024 * 1024
    data = {"type": "FeatureCollection", "features": []}
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        log.warning("Airspace path %r could not be resolved (%s); serving empty", path, exc)
    else:
        if not resolved.is_file():
            log.warning("Airspace path %s is not a regular file; serving empty", resolved)
        else:
            try:
                size = resolved.stat().st_size
                if size > _AIRSPACE_MAX_BYTES:
                    log.warning("Airspace file %s is %d bytes — over %d-byte limit; serving empty",
                                resolved, size, _AIRSPACE_MAX_BYTES)
                else:
                    with open(resolved) as fh:
                        data = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Failed to load airspace from %s: %s", resolved, exc)

    cache._set_cache("airspace", data)
    return data
