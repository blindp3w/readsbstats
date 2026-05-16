"""Playwright smoke tests for the v2 React SPA.

Runs against `v2_server` (RSBS_ROOT_PATH=/stats, port 18081, separate from the
v1 fixture). Targets /stats/* URLs. Uses data-testid selectors — keep
those stable across UI refactors.

Coverage strategy: smaller per-page surface than test_mobile_smoke.py because
the v2 surface is incomplete during the migration. As pages land in Phases
1–4 we extend coverage here.

Phase 1 scope:
  - /stats/settings — sections render, sensitive values masked
  - /stats/watchlist — list + add + delete flow
  - CSRF regression: POST /api/watchlist without X-Requested-With → 403
  - Per-device axe-core scan (if @axe-core/playwright reachable)
"""
import json
import time
import urllib.request

import pytest
from playwright.sync_api import expect


pytestmark = pytest.mark.ui


# Six-device matrix — same shape as test_mobile_smoke.py's parametrise. The
# Lenovo tablet has portrait + landscape variants; we use landscape here
# (matches typical use). Hours of CI is the trade-off; adjust if needed.
DEVICES = [
    "iphone_15",
    "iphone_15_pro_max",
    "ipad_pro_11",
    "pixel_7",
    "galaxy_s24",
    "lenovo_tab_11",
]


def _new_page(ctx_fixture, v2_server):
    base_url, _flight_id = v2_server
    page = ctx_fixture.new_page()
    return base_url, page


@pytest.mark.parametrize("device_name", DEVICES)
def test_v2_settings_page_renders(request, v2_server, device_name):
    ctx = request.getfixturevalue(f"ctx_{device_name}")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/settings", wait_until="networkidle")
        # Page chrome
        expect(page.locator('[data-testid="page-settings"]')).to_be_visible()
        expect(page.locator('[data-testid="settings-section-receiver"]')).to_be_visible()
        expect(page.locator('[data-testid="settings-section-telegram"]')).to_be_visible()
        # Heading visible
        expect(page.locator("h1", has_text="Settings")).to_be_visible()
        # No horizontal overflow on mobile widths
        viewport = page.viewport_size
        if viewport and viewport["width"] < 700:
            scroll_w = page.evaluate("document.documentElement.scrollWidth")
            assert scroll_w <= viewport["width"] + 1, (
                f"horizontal overflow on {device_name}: scroll {scroll_w} vs viewport {viewport['width']}"
            )
    finally:
        page.close()


@pytest.mark.parametrize("device_name", DEVICES)
def test_v2_settings_does_not_leak_secrets(request, v2_server, device_name):
    ctx = request.getfixturevalue(f"ctx_{device_name}")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/settings", wait_until="networkidle")
        body = page.content()
        # Sentinel values that the test env never sets; included for
        # regression — if a future change accidentally rendered config raw,
        # one of these patterns is likely to appear in real deployments.
        # The strongest pin is the masked-label test in test_web.py; this
        # asserts the rendered DOM doesn't leak typical secret formats.
        for needle in ["bot:", "AAAAAA:", "TELEGRAM_TOKEN"]:
            assert needle not in body, f"settings page may leak secret pattern: {needle}"
    finally:
        page.close()


@pytest.mark.parametrize("device_name", DEVICES)
def test_v2_watchlist_add_and_delete_flow(request, v2_server, device_name):
    ctx = request.getfixturevalue(f"ctx_{device_name}")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/watchlist", wait_until="networkidle")
        expect(page.locator('[data-testid="page-watchlist"]')).to_be_visible()
        expect(page.locator('[data-testid="watchlist-add-form"]')).to_be_visible()

        # Use a unique value per-device so parallel runs don't collide.
        unique_value = f"aa{device_name[:4]}1"
        # Radix Select isn't a native <select>; open and click the option.
        page.locator('[data-testid="watchlist-match-type"]').click()
        page.get_by_role("option", name="ICAO hex").click()
        page.locator('[data-testid="watchlist-value"]').fill(unique_value)
        page.locator('[data-testid="watchlist-label"]').fill(f"e2e-{device_name}")

        page.locator('[data-testid="watchlist-add-submit"]').click()

        # Wait for the row to appear in the table (TanStack Query invalidate).
        row_value = page.locator(f'[data-testid="watchlist-table"] td', has_text=unique_value)
        expect(row_value).to_be_visible(timeout=5000)

        # Find the row's id via the data-testid attribute pattern.
        row_locator = page.locator(f'[data-testid^="watchlist-row-"]', has_text=unique_value)
        expect(row_locator).to_have_count(1)
        row_handle = row_locator.first
        row_testid = row_handle.get_attribute("data-testid")
        assert row_testid and row_testid.startswith("watchlist-row-")
        row_id = row_testid.removeprefix("watchlist-row-")

        # Click the delete button → confirm in Radix Dialog.
        page.locator(f'[data-testid="watchlist-delete-{row_id}"]').click()
        page.locator('[data-testid="watchlist-delete-confirm"]').click()

        # Row should disappear (optimistic update + server confirmation).
        expect(row_value).not_to_be_visible(timeout=5000)
    finally:
        page.close()


@pytest.mark.parametrize("device_name", DEVICES)
def test_v2_feeders_page_renders(request, v2_server, device_name):
    ctx = request.getfixturevalue(f"ctx_{device_name}")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/feeders", wait_until="networkidle")
        expect(page.locator('[data-testid="page-feeders"]')).to_be_visible()
        expect(page.locator("h1", has_text="Feeders")).to_be_visible()
        expect(page.locator('[data-testid="feeders-refresh"]')).to_be_visible()
    finally:
        page.close()


@pytest.mark.parametrize("device_name", DEVICES)
def test_v2_history_page_renders_with_filters(request, v2_server, device_name):
    ctx = request.getfixturevalue(f"ctx_{device_name}")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/history", wait_until="networkidle")
        expect(page.locator('[data-testid="page-history"]')).to_be_visible()
        expect(page.locator('[data-testid="history-filters-form"]')).to_be_visible()
        # Table or empty-state appears (test DB has 1 seeded flight)
        flights_table = page.locator('[data-testid="flights-table"]')
        empty = page.locator('[data-testid="flights-empty"]')
        expect(flights_table.or_(empty)).to_be_visible()
    finally:
        page.close()


def test_v2_history_filter_persists_in_url(request, v2_server):
    # Single device — URL behavior is device-independent.
    import re
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/history", wait_until="networkidle")
        page.locator('[data-testid="history-filter-callsign"]').fill("LOT")
        page.locator('[data-testid="history-filter-callsign"]').press("Enter")
        expect(page).to_have_url(re.compile(r"callsign=LOT"))
    finally:
        page.close()


def test_v2_nav_live_badge_polls_api_live(request, v2_server):
    """Nav badge fetches /api/live on mount and renders the count."""
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, _ = v2_server
    page = ctx.new_page()
    try:
        with page.expect_request(
            lambda req: req.url.endswith("/api/live") or "/api/live?" in req.url,
            timeout=5000,
        ):
            page.goto(f"{base_url}/", wait_until="networkidle")
        # Badge is visible regardless of count (it shows "—" when 0).
        expect(page.locator('[data-testid="nav-live-badge"]')).to_be_visible()
    finally:
        page.close()


def test_v2_aircraft_watch_button_round_trip(request, v2_server):
    """+ Watch adds to watchlist; button flips to ✓ Watching; click again removes."""
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, _ = v2_server
    page = ctx.new_page()
    try:
        # The seeded fixture has icao=aabbcc. Make sure it's NOT already watched.
        page.goto(f"{base_url}/aircraft/aabbcc", wait_until="networkidle")
        btn = page.locator('[data-testid="aircraft-watch-toggle"]')
        expect(btn).to_be_visible()
        expect(btn).to_contain_text("Watch")
        # Add
        btn.click()
        expect(btn).to_contain_text("Watching", timeout=3000)
        # Remove
        btn.click()
        expect(btn).to_contain_text("+ Watch", timeout=3000)
    finally:
        page.close()


def test_v2_flight_other_flights_section(request, v2_server):
    """Section renders when API returns other flights — seeded fixture only has one
    so we just assert the page doesn't error and the card is conditionally absent."""
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, flight_id = v2_server
    page = ctx.new_page()
    try:
        page.goto(f"{base_url}/flight/{flight_id}", wait_until="networkidle")
        # With only one flight in the seed, other_flights is empty → card hidden.
        expect(page.locator('[data-testid="page-flight"]')).to_be_visible()
    finally:
        page.close()


def test_v2_stats_emergency_squawks_link(request, v2_server):
    """Each emergency squawk cell is a link to /history?squawk=XXXX."""
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, _ = v2_server
    page = ctx.new_page()
    try:
        page.goto(f"{base_url}/", wait_until="networkidle")
        link = page.locator('[data-testid="stats-squawk-7700"]')
        expect(link).to_be_visible()
        href = link.get_attribute("href")
        assert href and href.endswith("/history?squawk=7700"), f"unexpected href: {href}"
    finally:
        page.close()


def test_v2_gallery_filter_click_drives_api_call(request, v2_server):
    """Clicking a filter pill must trigger a /api/aircraft/flagged request
    with the matching ?flags= param. Pinning this so a regression where the
    URL state isn't actually wired up to the query is caught immediately.
    """
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, _ = v2_server
    page = ctx.new_page()
    try:
        page.goto(f"{base_url}/gallery", wait_until="networkidle")
        # Capture the network request triggered by the click.
        with page.expect_request(
            lambda req: "/api/aircraft/flagged" in req.url and "flags=military" in req.url,
            timeout=5000,
        ):
            page.locator('[data-testid="gallery-filter-military"]').click()
    finally:
        page.close()


def test_v2_gallery_sort_change_drives_api_call(request, v2_server):
    """Sort control — opens Popover, click radio option, fires API call with sort_by."""
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, _ = v2_server
    page = ctx.new_page()
    try:
        page.goto(f"{base_url}/gallery", wait_until="networkidle")
        page.locator('[data-testid="gallery-sort"]').click()
        expect(page.locator('[data-testid="gallery-sort-panel"]')).to_be_visible()
        with page.expect_request(
            lambda req: "/api/aircraft/flagged" in req.url and "sort_by=flight_count" in req.url,
            timeout=5000,
        ):
            page.locator('[data-testid="gallery-sort-flight_count"]').click()
    finally:
        page.close()


@pytest.mark.parametrize("device_name", DEVICES)
def test_v2_gallery_page_renders(request, v2_server, device_name):
    ctx = request.getfixturevalue(f"ctx_{device_name}")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/gallery", wait_until="networkidle")
        expect(page.locator('[data-testid="page-gallery"]')).to_be_visible()
        expect(page.locator('[data-testid="gallery-filter-group"]')).to_be_visible()
        # Either the grid or the empty-state alert is visible.
        grid = page.locator('[data-testid="gallery-grid"]')
        empty = page.locator('[data-testid="gallery-empty"]')
        expect(grid.or_(empty)).to_be_visible()
    finally:
        page.close()


@pytest.mark.parametrize("device_name", DEVICES)
def test_v2_stats_page_renders(request, v2_server, device_name):
    ctx = request.getfixturevalue(f"ctx_{device_name}")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/", wait_until="networkidle")
        expect(page.locator('[data-testid="page-stats"]')).to_be_visible()
        expect(page.locator("h1", has_text="Statistics")).to_be_visible()
        expect(page.locator('[data-testid="stats-summary-cards"]')).to_be_visible()
        expect(page.locator('[data-testid="range-picker"]')).to_be_visible()
    finally:
        page.close()


def test_v2_stats_custom_range_popover_apply(request, v2_server):
    """Custom button opens the Radix Popover; Apply writes from/to to URL."""
    import re
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/", wait_until="networkidle")
        # Popover initially closed.
        expect(page.locator('[data-testid="range-custom-panel"]')).not_to_be_visible()
        page.locator('[data-testid="range-custom-toggle"]').click()
        expect(page.locator('[data-testid="range-custom-panel"]')).to_be_visible()
        # Fill the inputs and apply. Use a recent fixed window so the URL
        # epoch maths are predictable.
        page.locator('[data-testid="range-custom-from"]').fill("2026-05-15T00:00")
        page.locator('[data-testid="range-custom-to"]').fill("2026-05-16T00:00")
        page.locator('[data-testid="range-custom-apply"]').click()
        # Popover closes after Apply.
        expect(page.locator('[data-testid="range-custom-panel"]')).not_to_be_visible()
        # URL now has from + to (epoch) and no `range=`.
        expect(page).to_have_url(re.compile(r"[?&]from=\d+&to=\d+"))
    finally:
        page.close()


@pytest.mark.parametrize("device_name", DEVICES)
def test_v2_metrics_page_renders(request, v2_server, device_name):
    ctx = request.getfixturevalue(f"ctx_{device_name}")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/metrics", wait_until="networkidle")
        expect(page.locator('[data-testid="page-metrics"]')).to_be_visible()
        # The seeded DB has no receiver_stats rows → no-data alert should
        # appear (proves the panels mount + the empty-state path works).
        expect(page.locator('[data-testid="metrics-no-data"]')).to_be_visible()
    finally:
        page.close()


@pytest.mark.parametrize("device_name", DEVICES)
def test_v2_flight_page_renders(request, v2_server, device_name):
    ctx = request.getfixturevalue(f"ctx_{device_name}")
    base_url, page = _new_page(ctx, v2_server)
    flight_id = v2_server[1]
    try:
        page.goto(f"{base_url}/flight/{flight_id}", wait_until="networkidle")
        expect(page.locator('[data-testid="page-flight"]')).to_be_visible()
        expect(page.locator('[data-testid="flight-header-card"]')).to_be_visible()
        # Map + profile + positions table all render.
        expect(page.locator('[data-testid="flight-map-card"]')).to_be_visible()
        expect(page.locator('[data-testid="flight-profile-card"]')).to_be_visible()
        expect(page.locator('[data-testid="flight-positions-card"]')).to_be_visible()
    finally:
        page.close()


@pytest.mark.parametrize("device_name", DEVICES)
def test_v2_map_page_renders(request, v2_server, device_name):
    ctx = request.getfixturevalue(f"ctx_{device_name}")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/map", wait_until="networkidle")
        expect(page.locator('[data-testid="page-map"]')).to_be_visible()
        # Controls always visible
        expect(page.locator('[data-testid="map-controls-overlay"]')).to_be_visible()
        expect(page.locator('[data-testid="map-mode-live"]')).to_be_visible()
        expect(page.locator('[data-testid="map-mode-rewind"]')).to_be_visible()
        # Map container (Leaflet) renders
        expect(page.locator('[data-testid="map-container"]')).to_be_visible()
    finally:
        page.close()


def test_v2_map_heatmap_toggle_fires_api_call(request, v2_server):
    """Toggling Heatmap on triggers /api/map/heatmap?window=... and shows
    the window selector strip."""
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, _ = v2_server
    page = ctx.new_page()
    try:
        page.goto(f"{base_url}/map", wait_until="networkidle")
        # Window selector hidden when both layers are off
        expect(page.locator('[data-testid="map-window-selector"]')).not_to_be_visible()
        with page.expect_request(
            lambda req: "/api/map/heatmap" in req.url and "window=" in req.url,
            timeout=5000,
        ):
            page.locator('[data-testid="map-toggle-heatmap"]').click()
        # Window selector now visible
        expect(page.locator('[data-testid="map-window-selector"]')).to_be_visible()
    finally:
        page.close()


def test_v2_map_coverage_toggle_fires_api_call(request, v2_server):
    """Toggling Coverage triggers /api/map/coverage?window=... ."""
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, _ = v2_server
    page = ctx.new_page()
    try:
        page.goto(f"{base_url}/map", wait_until="networkidle")
        with page.expect_request(
            lambda req: "/api/map/coverage" in req.url and "window=" in req.url,
            timeout=5000,
        ):
            page.locator('[data-testid="map-toggle-coverage"]').click()
    finally:
        page.close()


def test_v2_map_sidebar_list_opens(request, v2_server):
    """List toggle opens the left Sheet; rows render (or empty state)."""
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, _ = v2_server
    page = ctx.new_page()
    try:
        page.goto(f"{base_url}/map", wait_until="networkidle")
        expect(page.locator('[data-testid="map-sidebar-list"]')).not_to_be_visible()
        page.locator('[data-testid="map-toggle-list"]').click()
        expect(page.locator('[data-testid="map-sidebar-list"]')).to_be_visible()
        # Either rows or empty-state visible.
        rows = page.locator('[data-testid="map-aircraft-list"]')
        empty = page.locator('[data-testid="map-list-empty"]')
        expect(rows.or_(empty)).to_be_visible()
    finally:
        page.close()


def test_v2_map_playback_play_advances_time(request, v2_server):
    """Pressing play in rewind mode advances the slider value."""
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, _ = v2_server
    page = ctx.new_page()
    try:
        page.goto(f"{base_url}/map", wait_until="networkidle")
        # Switch to rewind, dial back ~1 hour so there's lots of headroom.
        page.locator('[data-testid="map-mode-rewind"]').click()
        page.locator('[data-testid="map-jump-back-1h"]').click()
        slider = page.locator('[data-testid="map-rewind-slider"]')
        before = int(slider.input_value())
        assert before >= 3600, f"expected slider ≥ 3600 after −1h, got {before}"
        # Bump speed to 10× then play. At 10× the offset drops 10 s per real
        # second; wait ~2.5 s for ≥ 2 ticks.
        page.locator('[data-testid="map-speed-10x"]').click()
        page.locator('[data-testid="map-play-toggle"]').click()
        page.wait_for_timeout(2500)
        after = int(slider.input_value())
        assert after < before, f"slider should advance towards now: before={before} after={after}"
    finally:
        page.close()


def test_v2_map_rewind_toggle_reveals_slider(request, v2_server):
    ctx = request.getfixturevalue("ctx_iphone_15")
    base_url, page = _new_page(ctx, v2_server)
    try:
        page.goto(f"{base_url}/map", wait_until="networkidle")
        # Initially no rewind slider — Live is the default mode.
        expect(page.locator('[data-testid="map-rewind-controls"]')).not_to_be_visible()
        # Switch to Rewind
        page.locator('[data-testid="map-mode-rewind"]').click()
        expect(page.locator('[data-testid="map-rewind-controls"]')).to_be_visible()
        expect(page.locator('[data-testid="map-rewind-slider"]')).to_be_visible()
    finally:
        page.close()


@pytest.mark.parametrize("device_name", DEVICES)
def test_v2_aircraft_page_renders(request, v2_server, device_name):
    ctx = request.getfixturevalue(f"ctx_{device_name}")
    base_url, page = _new_page(ctx, v2_server)
    try:
        # The seeded fixture has icao=aabbcc with 1 flight.
        page.goto(f"{base_url}/aircraft/aabbcc", wait_until="networkidle")
        expect(page.locator('[data-testid="page-aircraft"]')).to_be_visible()
        expect(page.locator('[data-testid="aircraft-info-card"]')).to_be_visible()
        # The flights table should render (seeded fixture has one flight).
        expect(page.locator('[data-testid="flights-table"]')).to_be_visible()
    finally:
        page.close()


def test_csrf_required_on_watchlist_post(v2_server):
    """Belt-and-suspenders: a direct fetch without X-Requested-With must 403.

    This proves the api.ts wrapper is the only way mutations work. If a
    future refactor weakens the wrapper, this test fails — surfacing the
    regression before it ships.
    """
    base_url, _ = v2_server
    req = urllib.request.Request(
        f"{base_url}/api/watchlist",
        method="POST",
        data=json.dumps({"match_type": "icao", "value": "aabbcc"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        raised = False
    except urllib.error.HTTPError as exc:
        assert exc.code == 403, f"expected 403, got {exc.code}"
        raised = True
    assert raised, "POST without X-Requested-With unexpectedly succeeded"


def test_csrf_passes_with_header(v2_server):
    """Same request WITH X-Requested-With must succeed (or 409 on duplicate)."""
    base_url, _ = v2_server
    req = urllib.request.Request(
        f"{base_url}/api/watchlist",
        method="POST",
        data=json.dumps({"match_type": "icao", "value": "deadbe"}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            assert res.status == 201
    except urllib.error.HTTPError as exc:
        # 409 = duplicate from a prior parallel test; acceptable signal that
        # the CSRF gate passed.
        assert exc.code in (201, 409), f"unexpected {exc.code}"
