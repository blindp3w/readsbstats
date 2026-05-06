"""
Playwright mobile/tablet smoke tests.

Run with: pytest tests/ui/ -v -m ui

7 devices × 5 pages = 35 tests.
Pages: / (statistics), /map, /live (redirect), /history, /flight/{id}

Hamburger nav breakpoint: ≤1100px CSS width.
Filters-toggle breakpoint:  ≤600px CSS width.

Device categories:
  Phone (≤600px):                      hamburger=True,  filters=True
  Portrait tablet (601–1100px):        hamburger=True,  filters=False
  Landscape tablet / desktop (>1100px): hamburger=False, filters=False
"""
import pytest
from playwright.sync_api import Page, expect

from tests.ui.conftest import screenshot_path


def check_no_overflow(page: Page) -> None:
    # Map page sets overflow:hidden on body (sidebar slides off-screen intentionally).
    # Users can't scroll there, so layout overflow is not a UX problem — skip check.
    result = page.evaluate("""() => {
        if (window.getComputedStyle(document.body).overflowX === 'hidden') return null;
        const sw = document.documentElement.scrollWidth;
        const iw = window.innerWidth;
        return sw > iw + 2 ? {sw, iw} : null;
    }""")
    assert result is None, (
        f"Horizontal overflow: scrollWidth={result['sw']} innerWidth={result['iw']}"
    )


# ---------------------------------------------------------------------------
# / (statistics — default landing page)
# ---------------------------------------------------------------------------

class TestStatisticsPage:

    def _run(self, ctx, live_server, device_name, has_hamburger):
        base_url, _ = live_server
        page = ctx.new_page()
        try:
            resp = page.goto(f"{base_url}/", wait_until="domcontentloaded")
            assert resp.status == 200
            expect(page.locator("h1", has_text="Statistics")).to_be_visible()
            expect(page.locator(".summary-cards")).to_be_visible()
            if has_hamburger:
                expect(page.locator("#nav-toggle")).to_be_visible()
            check_no_overflow(page)
            page.screenshot(path=str(screenshot_path(device_name, "statistics")))
        finally:
            page.close()

    @pytest.mark.ui
    def test_iphone_15(self, ctx_iphone_15, live_server):
        self._run(ctx_iphone_15, live_server, "iphone_15", has_hamburger=True)

    @pytest.mark.ui
    def test_iphone_15_pro_max(self, ctx_iphone_15_pro_max, live_server):
        self._run(ctx_iphone_15_pro_max, live_server, "iphone_15_pro_max", has_hamburger=True)

    @pytest.mark.ui
    def test_ipad_pro_11(self, ctx_ipad_pro_11, live_server):
        self._run(ctx_ipad_pro_11, live_server, "ipad_pro_11", has_hamburger=True)

    @pytest.mark.ui
    def test_pixel_7(self, ctx_pixel_7, live_server):
        self._run(ctx_pixel_7, live_server, "pixel_7", has_hamburger=True)

    @pytest.mark.ui
    def test_galaxy_s24(self, ctx_galaxy_s24, live_server):
        self._run(ctx_galaxy_s24, live_server, "galaxy_s24", has_hamburger=True)

    @pytest.mark.ui
    def test_lenovo_tab_11(self, ctx_lenovo_tab_11, live_server):
        self._run(ctx_lenovo_tab_11, live_server, "lenovo_tab_11", has_hamburger=False)

    @pytest.mark.ui
    def test_lenovo_tab_11_portrait(self, ctx_lenovo_tab_11_portrait, live_server):
        self._run(ctx_lenovo_tab_11_portrait, live_server, "lenovo_tab_11_portrait", has_hamburger=True)


# ---------------------------------------------------------------------------
# /map
# ---------------------------------------------------------------------------

class TestMapPage:

    def _run(self, ctx, live_server, device_name):
        base_url, _ = live_server
        page = ctx.new_page()
        try:
            resp = page.goto(f"{base_url}/map", wait_until="domcontentloaded")
            assert resp.status == 200
            expect(page.locator("#map-full")).to_be_visible()
            expect(page.locator(".map-controls-overlay")).to_be_visible()
            expect(page.locator("#map-mode-live")).to_be_visible()
            expect(page.locator("#map-sidebar-toggle")).to_be_visible()
            expect(page.locator("a.brand")).to_be_visible()
            check_no_overflow(page)
            page.screenshot(path=str(screenshot_path(device_name, "map")))
        finally:
            page.close()

    @pytest.mark.ui
    def test_iphone_15(self, ctx_iphone_15, live_server):
        self._run(ctx_iphone_15, live_server, "iphone_15")

    @pytest.mark.ui
    def test_iphone_15_pro_max(self, ctx_iphone_15_pro_max, live_server):
        self._run(ctx_iphone_15_pro_max, live_server, "iphone_15_pro_max")

    @pytest.mark.ui
    def test_ipad_pro_11(self, ctx_ipad_pro_11, live_server):
        self._run(ctx_ipad_pro_11, live_server, "ipad_pro_11")

    @pytest.mark.ui
    def test_pixel_7(self, ctx_pixel_7, live_server):
        self._run(ctx_pixel_7, live_server, "pixel_7")

    @pytest.mark.ui
    def test_galaxy_s24(self, ctx_galaxy_s24, live_server):
        self._run(ctx_galaxy_s24, live_server, "galaxy_s24")

    @pytest.mark.ui
    def test_lenovo_tab_11(self, ctx_lenovo_tab_11, live_server):
        self._run(ctx_lenovo_tab_11, live_server, "lenovo_tab_11")

    @pytest.mark.ui
    def test_lenovo_tab_11_portrait(self, ctx_lenovo_tab_11_portrait, live_server):
        self._run(ctx_lenovo_tab_11_portrait, live_server, "lenovo_tab_11_portrait")


# ---------------------------------------------------------------------------
# /live → redirect to /map
# ---------------------------------------------------------------------------

class TestLiveRedirect:

    def _run(self, ctx, live_server, device_name):
        base_url, _ = live_server
        page = ctx.new_page()
        try:
            resp = page.goto(f"{base_url}/live", wait_until="domcontentloaded")
            assert resp.status == 200
            assert page.url.endswith("/map"), f"Expected redirect to /map, got {page.url}"
            expect(page.locator("#map-full")).to_be_visible()
            page.screenshot(path=str(screenshot_path(device_name, "live_redirect")))
        finally:
            page.close()

    @pytest.mark.ui
    def test_iphone_15(self, ctx_iphone_15, live_server):
        self._run(ctx_iphone_15, live_server, "iphone_15")

    @pytest.mark.ui
    def test_iphone_15_pro_max(self, ctx_iphone_15_pro_max, live_server):
        self._run(ctx_iphone_15_pro_max, live_server, "iphone_15_pro_max")

    @pytest.mark.ui
    def test_ipad_pro_11(self, ctx_ipad_pro_11, live_server):
        self._run(ctx_ipad_pro_11, live_server, "ipad_pro_11")

    @pytest.mark.ui
    def test_pixel_7(self, ctx_pixel_7, live_server):
        self._run(ctx_pixel_7, live_server, "pixel_7")

    @pytest.mark.ui
    def test_galaxy_s24(self, ctx_galaxy_s24, live_server):
        self._run(ctx_galaxy_s24, live_server, "galaxy_s24")

    @pytest.mark.ui
    def test_lenovo_tab_11(self, ctx_lenovo_tab_11, live_server):
        self._run(ctx_lenovo_tab_11, live_server, "lenovo_tab_11")

    @pytest.mark.ui
    def test_lenovo_tab_11_portrait(self, ctx_lenovo_tab_11_portrait, live_server):
        self._run(ctx_lenovo_tab_11_portrait, live_server, "lenovo_tab_11_portrait")


# ---------------------------------------------------------------------------
# /history  (served by index.html)
# ---------------------------------------------------------------------------

class TestHistoryPage:

    def _run(self, ctx, live_server, device_name, has_hamburger, has_filters):
        base_url, _ = live_server
        page = ctx.new_page()
        try:
            resp = page.goto(f"{base_url}/history", wait_until="domcontentloaded")
            assert resp.status == 200
            expect(page.locator("h1", has_text="Flight History")).to_be_visible()
            expect(page.locator("#search-form")).to_be_visible()
            expect(page.locator(".table-wrap")).to_be_visible()
            expect(page.locator("#flights-table")).to_be_visible()
            if has_hamburger:
                expect(page.locator("#nav-toggle")).to_be_visible()
            if has_filters:
                expect(page.locator("#filters-toggle")).to_be_visible()
            check_no_overflow(page)
            page.screenshot(path=str(screenshot_path(device_name, "history")))
        finally:
            page.close()

    @pytest.mark.ui
    def test_iphone_15(self, ctx_iphone_15, live_server):
        self._run(ctx_iphone_15, live_server, "iphone_15", has_hamburger=True, has_filters=True)

    @pytest.mark.ui
    def test_iphone_15_pro_max(self, ctx_iphone_15_pro_max, live_server):
        self._run(ctx_iphone_15_pro_max, live_server, "iphone_15_pro_max", has_hamburger=True, has_filters=True)

    @pytest.mark.ui
    def test_ipad_pro_11(self, ctx_ipad_pro_11, live_server):
        # 834px CSS width: hamburger at ≤1100px, but no filters-toggle (that's ≤600px only)
        self._run(ctx_ipad_pro_11, live_server, "ipad_pro_11", has_hamburger=True, has_filters=False)

    @pytest.mark.ui
    def test_pixel_7(self, ctx_pixel_7, live_server):
        self._run(ctx_pixel_7, live_server, "pixel_7", has_hamburger=True, has_filters=True)

    @pytest.mark.ui
    def test_galaxy_s24(self, ctx_galaxy_s24, live_server):
        self._run(ctx_galaxy_s24, live_server, "galaxy_s24", has_hamburger=True, has_filters=True)

    @pytest.mark.ui
    def test_lenovo_tab_11(self, ctx_lenovo_tab_11, live_server):
        # 1180px CSS width: above 1100px breakpoint — inline nav, no hamburger
        self._run(ctx_lenovo_tab_11, live_server, "lenovo_tab_11", has_hamburger=False, has_filters=False)

    @pytest.mark.ui
    def test_lenovo_tab_11_portrait(self, ctx_lenovo_tab_11_portrait, live_server):
        # ~800px CSS width: hamburger at ≤1100px, but no filters-toggle (>600px)
        self._run(ctx_lenovo_tab_11_portrait, live_server, "lenovo_tab_11_portrait", has_hamburger=True, has_filters=False)


# ---------------------------------------------------------------------------
# /flight/{id}
# ---------------------------------------------------------------------------

class TestFlightDetailPage:

    def _run(self, ctx, live_server, device_name, has_hamburger):
        base_url, flight_id = live_server
        page = ctx.new_page()
        try:
            resp = page.goto(f"{base_url}/flight/{flight_id}", wait_until="domcontentloaded")
            assert resp.status == 200
            expect(page.locator("#flight-title")).to_be_visible()
            expect(page.locator("#flight-map")).to_be_visible()
            expect(page.locator("#pos-table")).to_be_visible()
            expect(page.locator("#detail-meta")).to_be_visible()
            expect(page.locator("a.back-link")).to_be_visible()
            if has_hamburger:
                expect(page.locator("#nav-toggle")).to_be_visible()
            check_no_overflow(page)
            page.screenshot(path=str(screenshot_path(device_name, "flight_detail")))
        finally:
            page.close()

    @pytest.mark.ui
    def test_iphone_15(self, ctx_iphone_15, live_server):
        self._run(ctx_iphone_15, live_server, "iphone_15", has_hamburger=True)

    @pytest.mark.ui
    def test_iphone_15_pro_max(self, ctx_iphone_15_pro_max, live_server):
        self._run(ctx_iphone_15_pro_max, live_server, "iphone_15_pro_max", has_hamburger=True)

    @pytest.mark.ui
    def test_ipad_pro_11(self, ctx_ipad_pro_11, live_server):
        self._run(ctx_ipad_pro_11, live_server, "ipad_pro_11", has_hamburger=True)

    @pytest.mark.ui
    def test_pixel_7(self, ctx_pixel_7, live_server):
        self._run(ctx_pixel_7, live_server, "pixel_7", has_hamburger=True)

    @pytest.mark.ui
    def test_galaxy_s24(self, ctx_galaxy_s24, live_server):
        self._run(ctx_galaxy_s24, live_server, "galaxy_s24", has_hamburger=True)

    @pytest.mark.ui
    def test_lenovo_tab_11(self, ctx_lenovo_tab_11, live_server):
        self._run(ctx_lenovo_tab_11, live_server, "lenovo_tab_11", has_hamburger=False)

    @pytest.mark.ui
    def test_lenovo_tab_11_portrait(self, ctx_lenovo_tab_11_portrait, live_server):
        self._run(ctx_lenovo_tab_11_portrait, live_server, "lenovo_tab_11_portrait", has_hamburger=True)
