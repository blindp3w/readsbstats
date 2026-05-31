"""DuckDB-backed accelerator for full-table analytical scans.

Read-only ATTACH against the live SQLite history.db via `sqlite_scanner`.
SQLite remains the only write path; this module is invoked from the web
process exclusively (the collector has no analytical workload — see
the plan doc and `What NOT to do` section there).

Public surface:
- `is_available()` — gated by infra check (memoised) + config.USE_DUCKDB
  (re-read each call so the env flag can flip without restart in tests).
- `heatmap()` and `coverage()` — the two ported aggregates; return
  shapes match what the SQLite branch in web.py produces. Return `None`
  on per-query failure so the caller can fall through to SQLite.
- `close()` — called from FastAPI lifespan shutdown.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
from pathlib import Path

from . import config

# Audit 2026-05-26: cleanup must only target a directory readsbstats owns.
# The marker file is created on first init in an empty/new temp dir; later
# inits refuse to scan or delete anything from a non-empty dir without it.
_TEMP_MARKER = ".readsbstats-duckdb-tmp"

# Only filenames matching these patterns are eligible for cleanup, even
# inside an owned directory. Prevents collateral damage if a user mounts
# a useful directory at the same path.
_DUCKDB_TEMP_PATTERNS = (
    "duckdb_temporary_*",
    "*.parquet.tmp",
    "*.tmp",
)

# Paths that must never be used as the temp dir, even if writable. The
# match is exact or "is a subdir of"; both cases would let `os.scandir`
# walk into directories that contain unrelated user data.
_DANGEROUS_TEMP_PREFIXES = ("/", "/tmp", "/var/tmp", "/home", "/root", "/etc", "/var")


def _is_dangerous_temp_dir(p: str) -> bool:
    """Check whether ``p`` is one of the always-unsafe parent paths.

    Compares both the literal string and the symlink-resolved form
    against the denylist — on macOS ``/home`` resolves to
    ``/System/Volumes/Data/home`` via autofs, and we want to refuse the
    user-typed form regardless.
    """
    if not p:
        return True
    candidates = {p.rstrip("/")}
    try:
        candidates.add(str(Path(p).resolve()).rstrip("/"))
    except OSError:
        return True
    for bad in _DANGEROUS_TEMP_PREFIXES:
        if bad in candidates:
            return True
    # Don't reject subdirs of /tmp — pytest uses tmp_path under
    # /private/var/folders on macOS and /tmp on Linux, which is fine.
    return False


def _cleanup_owned_temp_dir(temp_dir: Path) -> None:
    """Delete only DuckDB-pattern files from an owned temp dir.

    Owned = the marker file exists. If absent and the directory is
    non-empty, refuse to touch anything. If absent and the directory is
    empty, write the marker so future inits recognise the dir as ours."""
    if not temp_dir.is_dir():
        return
    marker = temp_dir / _TEMP_MARKER
    if not marker.exists():
        # Don't touch anything in a directory we don't own.
        try:
            has_content = any(temp_dir.iterdir())
        except OSError:
            return
        if has_content:
            log.warning(
                "analytics: DUCKDB_TEMP_DIR=%s exists but has no %s marker — "
                "refusing to scan/delete. Either point RSBS_DUCKDB_TEMP_DIR at "
                "an empty dir or delete the contents manually.",
                temp_dir, _TEMP_MARKER,
            )
            raise OSError("temp dir not owned by readsbstats")
        try:
            marker.touch()
        except OSError:
            return
        return
    for entry in os.scandir(temp_dir):
        if not entry.is_file():
            continue
        name = entry.name
        if name == _TEMP_MARKER:
            continue
        if not any(fnmatch.fnmatch(name, pat) for pat in _DUCKDB_TEMP_PATTERNS):
            continue
        try:
            os.unlink(entry.path)
        except OSError:
            pass

try:
    import duckdb
    _DUCKDB_IMPORT_OK = True
except ImportError:
    duckdb = None
    _DUCKDB_IMPORT_OK = False

log = logging.getLogger(__name__)

_CONN = None
_INIT_LOCK = threading.Lock()
_INFRA_OK: bool | None = None
_LOGGED_INIT = False
_SHUTDOWN = threading.Event()  # set by close(); suppresses in-flight query warnings


def _is_safe_sql_path(p: str) -> bool:
    """DuckDB's ATTACH and SET temp_directory take string literals with no
    parameter binding, so the path becomes part of the SQL text. Reject
    any character that could break out of the surrounding single quotes."""
    if not p:
        return False
    return not any(c in p for c in ("'", '"', ";", "\x00", "\n", "\r"))


def _quote_sql_string(s: str) -> str:
    """Wrap *s* as a single-quoted SQL string literal. Only safe to call
    after `_is_safe_sql_path` has accepted the value."""
    return "'" + s + "'"


def _init_connection() -> None:
    """Single-shot connection setup. Caller must hold `_INIT_LOCK`."""
    global _CONN, _INFRA_OK, _LOGGED_INIT

    if not _DUCKDB_IMPORT_OK:
        _INFRA_OK = False
        if not _LOGGED_INIT:
            log.info("duckdb not installed, falling back to SQLite for analytics")
            _LOGGED_INIT = True
        return

    db_path = config.DB_PATH
    temp_dir = config.DUCKDB_TEMP_DIR
    home_dir = config.DUCKDB_HOME_DIR

    if (not _is_safe_sql_path(db_path)
            or not _is_safe_sql_path(temp_dir)
            or not _is_safe_sql_path(home_dir)):
        _INFRA_OK = False
        log.warning(
            "analytics: rejecting DuckDB init — DB_PATH / DUCKDB_TEMP_DIR / "
            "DUCKDB_HOME_DIR contains characters unsafe to embed in SQL"
        )
        return

    if _is_dangerous_temp_dir(temp_dir):
        _INFRA_OK = False
        log.warning(
            "analytics: refusing DuckDB init — DUCKDB_TEMP_DIR=%s is a "
            "shared system directory; point RSBS_DUCKDB_TEMP_DIR at a "
            "dedicated subdirectory the readsbstats user owns",
            temp_dir,
        )
        return

    try:
        Path(home_dir).mkdir(parents=True, exist_ok=True, mode=0o750)
        Path(temp_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        _cleanup_owned_temp_dir(Path(temp_dir))
    except OSError as exc:
        _INFRA_OK = False
        log.warning("analytics: cannot prepare DuckDB dirs: %s", exc)
        return

    try:
        conn = duckdb.connect(":memory:")
        # Home directory MUST be set before INSTALL — DuckDB resolves it on
        # first use and the readsbstats system user has no /home.
        conn.execute(f"SET home_directory={_quote_sql_string(home_dir)}")
        # INSTALL is best-effort: it's a no-op if the extension is already
        # present and fails gracefully when there is no network. LOAD is the
        # real gate — if the extension is not available it raises here and the
        # outer except disables the engine with a single warning.
        try:
            conn.execute("INSTALL sqlite_scanner")
        except Exception:  # noqa: BLE001 — no network / already installed
            pass
        conn.execute("LOAD sqlite_scanner")
        conn.execute(f"SET memory_limit='{int(config.DUCKDB_MEMORY_MB)}MB'")
        conn.execute(f"SET threads={int(config.DUCKDB_THREADS)}")
        conn.execute(f"SET temp_directory={_quote_sql_string(temp_dir)}")
        conn.execute(
            f"ATTACH {_quote_sql_string(db_path)} AS hist (TYPE SQLITE, READ_ONLY)"
        )
    except Exception as exc:  # noqa: BLE001 — engine-wide failure, log and disable
        _INFRA_OK = False
        log.warning("analytics: DuckDB init failed (%s), falling back to SQLite",
                    type(exc).__name__)
        return

    _CONN = conn
    _INFRA_OK = True
    if not _LOGGED_INIT:
        log.info(
            "analytics: duckdb attached, threads=%d, memory_limit=%dMB",
            config.DUCKDB_THREADS, config.DUCKDB_MEMORY_MB,
        )
        _LOGGED_INIT = True


def _ensure_initialised() -> bool:
    """Lazily run `_init_connection` exactly once per process. Returns
    True iff `_CONN` is now attached."""
    if _INFRA_OK is not None:
        return _INFRA_OK is True
    with _INIT_LOCK:
        if _INFRA_OK is None:
            _init_connection()
        return _INFRA_OK is True


def is_available() -> bool:
    """True iff the DuckDB engine is wired up AND `config.USE_DUCKDB` is on.

    Re-reads `config.USE_DUCKDB` on every call so tests can flip the
    flag without restarting the process. The expensive infra check
    (extension + ATTACH) only runs once per process.
    """
    if _SHUTDOWN.is_set():
        return False
    if not config.USE_DUCKDB:
        return False
    return _ensure_initialised()


def heatmap(cutoff_ts: int | None, precision: int) -> list[tuple[float, float, int]] | None:
    """Return `(rlat, rlon, count)` cells from `hist.positions`. Mirrors
    the SQLite query in api.map._compute_heatmap_sync. Returns `None` on
    per-query failure so the caller can fall back to SQLite."""
    if not is_available() or _CONN is None:
        return None
    try:
        cur = _CONN.cursor()
        if _SHUTDOWN.is_set():
            return None
        # improvements.md A13-019: SQLite's `round()` is half-away-from-zero
        # but DuckDB's is banker's (half-to-even).  For values like
        # `lat=53.05, precision=1`, the two engines would land in different
        # buckets.  Group on an explicit integer bucket
        # (`FLOOR(x * 10^p + 0.5)`) and divide in Python so both engines
        # produce the same bucket assignment without floating-point drift
        # on the divide step.
        scale = 10 ** precision
        if cutoff_ts is None:
            rows = cur.execute(
                "SELECT CAST(FLOOR(lat * ? + 0.5) AS INTEGER) AS lat_bucket, "
                "       CAST(FLOOR(lon * ? + 0.5) AS INTEGER) AS lon_bucket, "
                "       COUNT(*) AS w "
                "FROM hist.positions "
                "WHERE lat IS NOT NULL AND lon IS NOT NULL "
                "GROUP BY lat_bucket, lon_bucket",
                [scale, scale],
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT CAST(FLOOR(lat * ? + 0.5) AS INTEGER) AS lat_bucket, "
                "       CAST(FLOOR(lon * ? + 0.5) AS INTEGER) AS lon_bucket, "
                "       COUNT(*) AS w "
                "FROM hist.positions "
                "WHERE lat IS NOT NULL AND lon IS NOT NULL AND ts > ? "
                "GROUP BY lat_bucket, lon_bucket",
                [scale, scale, int(cutoff_ts)],
            ).fetchall()
        return [(r[0] / scale, r[1] / scale, int(r[2])) for r in rows]
    except Exception:  # noqa: BLE001 — one bad query must not poison infra
        if _SHUTDOWN.is_set():
            log.debug("analytics.heatmap interrupted by shutdown")
        else:
            log.warning("analytics.heatmap failed; falling back to SQLite", exc_info=True)
        return None


def coverage(cutoff_ts: int | None, rlat: float, rlon: float,
             bucket_deg: int) -> dict[int, float] | None:
    """Return `{bucket_index: max_dist_nm}` from `hist.positions`.
    `bucket_deg` must divide 360 (the existing 10° in web.py gives 36
    buckets). `rlat` / `rlon` are receiver coordinates and come from
    config (not user input)."""
    if not is_available() or _CONN is None:
        return None
    num_buckets = 360 // int(bucket_deg)
    try:
        cur = _CONN.cursor()
        if _SHUTDOWN.is_set():
            return None
        # Audit-13 A13-076: use shared SQL helpers from geo.py. DuckDB
        # uses positional `?` params; helpers expand to the same shape
        # the old inline SQL had — bearing references rlon, rlat, rlat,
        # rlon and haversine references rlat × 3, rlon × 2 (see geo.py).
        from . import geo
        bearing_expr = geo.bearing_sql("lat", "lon", "?", "?")
        dist_expr    = geo.haversine_sql("lat", "lon", "?", "?")
        # Bearing: (rlon, rlat, rlat, rlon)
        # Haversine: (rlat, rlat, rlat, rlon, rlon)  (lat-ref × 3, lon-ref × 2)
        params: list = [rlon, rlat, rlat, rlon,
                        rlat, rlat, rlat, rlon, rlon]
        time_filter = ""
        if cutoff_ts is not None:
            time_filter = "AND ts > ? "
            params.append(int(cutoff_ts))
        sql = (
            "WITH pos_bearing AS ("
            f" SELECT {bearing_expr} AS bearing_deg,"
            f"        {dist_expr}    AS dist_nm"
            " FROM hist.positions"
            f" WHERE lat IS NOT NULL AND lon IS NOT NULL {time_filter}"
            ") "
            # NB: DuckDB's `CAST(double AS INTEGER)` uses banker's rounding;
            # SQLite truncates toward zero. We explicitly `FLOOR()` so both
            # engines yield the same bucket for non-integer bearings/buckets.
            f"SELECT (CAST(FLOOR(bearing_deg / {float(bucket_deg)}) AS INTEGER)) % {int(num_buckets)} AS bucket, "
            "MAX(dist_nm) AS max_dist "
            "FROM pos_bearing "
            "GROUP BY bucket"
        )
        rows = cur.execute(sql, params).fetchall()
        return {int(r[0]): float(r[1]) for r in rows}
    except Exception:  # noqa: BLE001
        if _SHUTDOWN.is_set():
            log.debug("analytics.coverage interrupted by shutdown")
        else:
            log.warning("analytics.coverage failed; falling back to SQLite", exc_info=True)
        return None


def close() -> None:
    """Close the DuckDB connection and sweep the temp dir. Called from
    FastAPI lifespan shutdown — also safe to call from tests."""
    global _CONN
    _SHUTDOWN.set()  # signal in-flight queries before closing the connection
    with _INIT_LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:  # noqa: BLE001
                log.debug("analytics: error closing DuckDB connection", exc_info=True)
            _CONN = None
        try:
            temp_dir = config.DUCKDB_TEMP_DIR
            if temp_dir and not _is_dangerous_temp_dir(temp_dir):
                _cleanup_owned_temp_dir(Path(temp_dir))
        except OSError:
            pass


def _reset_for_tests() -> None:
    """Drop all module state so the next call rebuilds from scratch.
    Tests use this between cases; production code never calls it."""
    global _CONN, _INFRA_OK, _LOGGED_INIT
    _SHUTDOWN.clear()
    with _INIT_LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:  # noqa: BLE001
                pass
        _CONN = None
        _INFRA_OK = None
        _LOGGED_INIT = False
