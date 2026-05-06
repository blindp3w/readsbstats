"""
Playwright mobile UI test fixtures.

Session-scoped live_server: starts a real uvicorn subprocess with a temp SQLite
database seeded with one flight + 3 positions, yields (base_url, flight_id),
and kills the process on teardown.

Seven device context fixtures cover the approved device set.
Two shared browser instances (one WebKit, one Chromium) are reused across all
device contexts of the same engine to reduce memory and startup overhead.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Playwright

UI_TEST_PORT = 18080
SERVER_STARTUP_TIMEOUT = 15


# ---------------------------------------------------------------------------
# DB seeding
# ---------------------------------------------------------------------------

def _seed_db(db_path: str) -> int:
    from readsbstats import database

    conn = database.connect(db_path)
    conn.executescript(database.DDL)
    database._migrate(conn)

    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO flights
            (icao_hex, callsign, registration, aircraft_type, squawk,
             first_seen, last_seen, max_alt_baro, max_gs, max_distance_nm,
             max_distance_bearing, total_positions, adsb_positions,
             mlat_positions, primary_source,
             lat_min, lat_max, lon_min, lon_max)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "aabbcc", "LOT123", "SP-LRA", "B738", None,
            now - 3600, now - 600,
            35000, 450.0, 150.0, 90.0,
            3, 3, 0, "adsb",
            51.9, 52.5, 20.8, 21.3,
        ),
    )
    conn.commit()
    flight_id = cur.lastrowid

    conn.executemany(
        "INSERT INTO positions (flight_id, ts, lat, lon, alt_baro, gs, source_type) VALUES (?,?,?,?,?,?,?)",
        [
            (flight_id, now - 3600, 52.10, 20.90, 30000, 420.0, "adsb_icao"),
            (flight_id, now - 2400, 52.25, 21.02, 35000, 450.0, "adsb_icao"),
            (flight_id, now -  600, 52.45, 21.25, 33000, 440.0, "adsb_icao"),
        ],
    )
    conn.commit()
    conn.close()
    return flight_id


def _wait_for_server(proc: subprocess.Popen, base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    health_url = f"{base_url}/api/health"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"Test server process exited early (code {proc.returncode}). "
                f"Port {UI_TEST_PORT} may already be in use."
            )
        try:
            if httpx.get(health_url, timeout=2.0).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Test server at {base_url} did not become ready within {timeout}s")


# ---------------------------------------------------------------------------
# Live server fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def live_server():
    with tempfile.TemporaryDirectory(prefix="rsbs_ui_") as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        flight_id = _seed_db(db_path)

        base_url = f"http://127.0.0.1:{UI_TEST_PORT}"
        env = {
            **os.environ,
            "RSBS_DB_PATH":   db_path,
            "RSBS_ROOT_PATH": "",
            "RSBS_LAT":       "52.24199",
            "RSBS_LON":       "21.02872",
        }

        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "readsbstats.web:app",
             "--host", "127.0.0.1", "--port", str(UI_TEST_PORT)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_server(proc, base_url, timeout=SERVER_STARTUP_TIMEOUT)
            yield base_url, flight_id
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
# Screenshot helper
# ---------------------------------------------------------------------------

_SCREENSHOT_BASE = Path(__file__).parent / "screenshots"


def screenshot_path(device_name: str, page_name: str) -> Path:
    d = _SCREENSHOT_BASE / device_name
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{page_name}.png"


# ---------------------------------------------------------------------------
# Shared browser instances (one per engine, reused across device contexts)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _webkit_browser(playwright: Playwright) -> Browser:
    browser = playwright.webkit.launch(headless=True)
    yield browser
    browser.close()


@pytest.fixture(scope="session")
def _chromium_browser(playwright: Playwright) -> Browser:
    browser = playwright.chromium.launch(headless=True)
    yield browser
    browser.close()


# ---------------------------------------------------------------------------
# Device context fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def ctx_iphone_15(_webkit_browser: Browser, playwright: Playwright):
    ctx = _webkit_browser.new_context(**playwright.devices["iPhone 15"])
    yield ctx
    ctx.close()


@pytest.fixture(scope="session")
def ctx_iphone_15_pro_max(_webkit_browser: Browser, playwright: Playwright):
    ctx = _webkit_browser.new_context(**playwright.devices["iPhone 15 Pro Max"])
    yield ctx
    ctx.close()


@pytest.fixture(scope="session")
def ctx_ipad_pro_11(_webkit_browser: Browser, playwright: Playwright):
    ctx = _webkit_browser.new_context(**playwright.devices["iPad Pro 11"])
    yield ctx
    ctx.close()


@pytest.fixture(scope="session")
def ctx_pixel_7(_chromium_browser: Browser, playwright: Playwright):
    ctx = _chromium_browser.new_context(**playwright.devices["Pixel 7"])
    yield ctx
    ctx.close()


@pytest.fixture(scope="session")
def ctx_galaxy_s24(_chromium_browser: Browser, playwright: Playwright):
    ctx = _chromium_browser.new_context(**playwright.devices["Galaxy S24"])
    yield ctx
    ctx.close()


@pytest.fixture(scope="session")
def ctx_lenovo_tab_11(_chromium_browser: Browser):
    ctx = _chromium_browser.new_context(
        viewport={"width": 1180, "height": 820},
        device_scale_factor=1.75,
        is_mobile=True,
        has_touch=True,
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; Lenovo TB336FU) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    yield ctx
    ctx.close()


@pytest.fixture(scope="session")
def ctx_lenovo_tab_11_portrait(_chromium_browser: Browser):
    ctx = _chromium_browser.new_context(
        viewport={"width": 800, "height": 1180},
        device_scale_factor=1.75,
        is_mobile=True,
        has_touch=True,
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; Lenovo TB336FU) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    yield ctx
    ctx.close()
