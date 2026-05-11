"""Tests for photo_sources.py — shared photo lookup chain + SSRF guards."""
from __future__ import annotations

import json
import socket
import time
import urllib.error
from unittest.mock import MagicMock

import pytest

from readsbstats import database, http_safe, photo_sources
from readsbstats.photo_sources import PhotoResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_resp(body: bytes | str, status: int = 200, url: str | None = None,
               headers: dict | None = None):
    """Minimal urllib response mock compatible with `with` blocks."""
    if isinstance(body, str):
        body = body.encode()
    mock = MagicMock()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    mock.read = MagicMock(return_value=body)
    mock.status = status
    mock.url = url
    mock.headers = headers or {}
    return mock


def _patch_opener(monkeypatch, mock_resp):
    """Patch the SSRF-safe opener directly so we don't trigger real DNS."""
    monkeypatch.setattr(
        http_safe._no_redirect_opener, "open",
        lambda req, timeout=None: mock_resp,
    )


def _patch_validate(monkeypatch, allow=True):
    """Bypass DNS/IP checks for source-level tests; failure path tests set allow=False."""
    if allow:
        monkeypatch.setattr(http_safe, "validate_url", lambda url: None)
    else:
        def _reject(url):
            raise ValueError("blocked by test")
        monkeypatch.setattr(http_safe, "validate_url", _reject)


def _patch_safe_open(monkeypatch, body, headers=None):
    """Patch _safe_open for source-fetcher unit tests."""
    if isinstance(body, str):
        body = body.encode()
    monkeypatch.setattr(
        photo_sources, "_safe_open",
        lambda url, *, timeout, max_bytes: (body, headers or {}),
    )


def _fake_addrinfo(ip: str):
    """Build a getaddrinfo result that returns a single given IP."""
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))]


# ---------------------------------------------------------------------------
# _validate_url / _ip_is_public
# ---------------------------------------------------------------------------

class TestValidateUrl:
    def test_https_with_public_ip_passes(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        photo_sources._validate_url("https://example.com/")

    def test_http_rejected(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("1.1.1.1"))
        with pytest.raises(ValueError, match="non-https"):
            photo_sources._validate_url("http://example.com/")

    def test_loopback_rejected(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("127.0.0.1"))
        with pytest.raises(ValueError, match="non-public"):
            photo_sources._validate_url("https://localhost/")

    def test_private_rfc1918_rejected(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("192.168.1.1"))
        with pytest.raises(ValueError, match="non-public"):
            photo_sources._validate_url("https://example.com/")

    def test_link_local_metadata_rejected(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("169.254.169.254"))
        with pytest.raises(ValueError, match="non-public"):
            photo_sources._validate_url("https://aws-meta/")

    def test_multicast_rejected(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo",
                            lambda h, p, **kw: _fake_addrinfo("224.0.0.1"))
        with pytest.raises(ValueError, match="non-public"):
            photo_sources._validate_url("https://multicast/")

    def test_dns_failure_rejected(self, monkeypatch):
        def _gai(*a, **kw):
            raise socket.gaierror("nodename nor servname provided")
        monkeypatch.setattr(socket, "getaddrinfo", _gai)
        with pytest.raises(ValueError, match="DNS resolution failed"):
            photo_sources._validate_url("https://nonexistent.invalid/")

    def test_no_host_rejected(self):
        with pytest.raises(ValueError, match="no host"):
            photo_sources._validate_url("https:///")


# ---------------------------------------------------------------------------
# _safe_open — size cap & redirect handling
# ---------------------------------------------------------------------------

class TestSafeOpen:
    def test_returns_body_and_headers(self, monkeypatch):
        _patch_validate(monkeypatch, allow=True)
        _patch_opener(monkeypatch, _mock_resp(b"hello",
                                              headers={"Content-Type": "text/plain"}))
        body, headers = photo_sources._safe_open(
            "https://example.com/", timeout=2, max_bytes=1024,
        )
        assert body == b"hello"
        assert headers["Content-Type"] == "text/plain"

    def test_oversize_response_rejected(self, monkeypatch):
        _patch_validate(monkeypatch, allow=True)
        _patch_opener(monkeypatch, _mock_resp(b"x" * 2000))
        with pytest.raises(ValueError, match="max_bytes"):
            photo_sources._safe_open(
                "https://example.com/", timeout=2, max_bytes=1024,
            )

    def test_propagates_validate_error(self, monkeypatch):
        _patch_validate(monkeypatch, allow=False)
        with pytest.raises(ValueError, match="blocked by test"):
            photo_sources._safe_open(
                "https://example.com/", timeout=2, max_bytes=1024,
            )

    def test_redirect_handler_raises_http_error(self):
        """_NoRedirectHandler.redirect_request must raise, not return a new request."""
        handler = photo_sources._NoRedirectHandler()
        req = MagicMock()
        req.full_url = "https://example.com/"
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(req, MagicMock(), 302, "Found", {},
                                      "https://169.254.169.254/")

    def test_post_flight_revalidation_on_url_change(self, monkeypatch):
        """If the response URL differs from the requested URL, revalidate it."""
        calls = []
        def fake_validate(url):
            calls.append(url)
        monkeypatch.setattr(http_safe, "validate_url", fake_validate)
        # Response reports a different (assumed public) URL
        _patch_opener(monkeypatch, _mock_resp(b"data",
                                              url="https://example.com/elsewhere"))
        photo_sources._safe_open(
            "https://example.com/", timeout=2, max_bytes=1024,
        )
        assert "https://example.com/" in calls
        assert "https://example.com/elsewhere" in calls


# ---------------------------------------------------------------------------
# Source fetchers — icao percent-encoding + payload parsing
# ---------------------------------------------------------------------------

class TestFetchPlanespotters:
    def test_success_returns_photo_result(self, monkeypatch):
        payload = {"photos": [{
            "thumbnail":       {"src": "https://ps.com/t.jpg"},
            "thumbnail_large": {"src": "https://ps.com/l.jpg"},
            "link":            "https://ps.com/p",
            "photographer":    "Alice",
        }]}
        _patch_safe_open(monkeypatch, json.dumps(payload))
        result = photo_sources._fetch_planespotters("aabbcc")
        assert result is not None
        assert result.thumbnail_url == "https://ps.com/t.jpg"
        assert result.large_url == "https://ps.com/l.jpg"
        assert result.link_url == "https://ps.com/p"
        assert result.photographer == "Alice"

    def test_empty_photos_returns_none(self, monkeypatch):
        _patch_safe_open(monkeypatch, json.dumps({"photos": []}))
        assert photo_sources._fetch_planespotters("aabbcc") is None

    def test_no_thumbnail_src_returns_none(self, monkeypatch):
        payload = {"photos": [{"thumbnail": {}, "link": None, "photographer": None}]}
        _patch_safe_open(monkeypatch, json.dumps(payload))
        assert photo_sources._fetch_planespotters("aabbcc") is None

    def test_icao_hex_is_percent_encoded(self, monkeypatch):
        captured = []
        def fake_open(url, *, timeout, max_bytes):
            captured.append(url)
            return json.dumps({"photos": []}).encode(), {}
        monkeypatch.setattr(photo_sources, "_safe_open", fake_open)
        photo_sources._fetch_planespotters("ab/../cd")
        assert "ab%2F..%2Fcd" in captured[0]
        assert "../" not in captured[0].split("/hex/")[-1]

    def test_network_error_propagates(self, monkeypatch):
        def _boom(url, *, timeout, max_bytes):
            raise OSError("timeout")
        monkeypatch.setattr(photo_sources, "_safe_open", _boom)
        with pytest.raises(OSError):
            photo_sources._fetch_planespotters("aabbcc")


class TestFetchAirportData:
    def test_success_returns_photo_result(self, monkeypatch):
        payload = {
            "status": 200,
            "data": [{"image": "https://ad.com/t.jpg", "link": "https://ad.com/p", "photographer": "Bob"}],
        }
        _patch_safe_open(monkeypatch, json.dumps(payload))
        result = photo_sources._fetch_airport_data("aabbcc")
        assert result is not None
        assert result.thumbnail_url == "https://ad.com/t.jpg"
        assert result.large_url == "https://ad.com/t.jpg"
        assert result.photographer == "Bob"

    def test_status_not_200_returns_none(self, monkeypatch):
        _patch_safe_open(monkeypatch, json.dumps({"status": 404, "data": []}))
        assert photo_sources._fetch_airport_data("aabbcc") is None

    def test_empty_data_array_returns_none(self, monkeypatch):
        _patch_safe_open(monkeypatch, json.dumps({"status": 200, "data": []}))
        assert photo_sources._fetch_airport_data("aabbcc") is None

    def test_missing_image_field_returns_none(self, monkeypatch):
        _patch_safe_open(monkeypatch, json.dumps({"status": 200, "data": [{"link": "https://ad.com/p"}]}))
        assert photo_sources._fetch_airport_data("aabbcc") is None

    def test_icao_hex_is_percent_encoded(self, monkeypatch):
        captured = []
        def fake_open(url, *, timeout, max_bytes):
            captured.append(url)
            return json.dumps({"status": 200, "data": []}).encode(), {}
        monkeypatch.setattr(photo_sources, "_safe_open", fake_open)
        photo_sources._fetch_airport_data("a&b=c")
        assert "a%26b%3Dc" in captured[0]


class TestFetchHexdb:
    def test_success_returns_photo_result(self, monkeypatch):
        _patch_safe_open(monkeypatch, "https://hexdb.io/img.jpg")
        result = photo_sources._fetch_hexdb("aabbcc")
        assert result is not None
        assert result.thumbnail_url == "https://hexdb.io/img.jpg"
        assert result.large_url == "https://hexdb.io/img.jpg"

    def test_na_response_returns_none(self, monkeypatch):
        _patch_safe_open(monkeypatch, "n/a")
        assert photo_sources._fetch_hexdb("aabbcc") is None

    def test_empty_response_returns_none(self, monkeypatch):
        _patch_safe_open(monkeypatch, "")
        assert photo_sources._fetch_hexdb("aabbcc") is None

    def test_http_404_returns_none(self, monkeypatch):
        def _raise_404(url, *, timeout, max_bytes):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        monkeypatch.setattr(photo_sources, "_safe_open", _raise_404)
        assert photo_sources._fetch_hexdb("aabbcc") is None

    def test_http_500_propagates(self, monkeypatch):
        def _raise_500(url, *, timeout, max_bytes):
            raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)
        monkeypatch.setattr(photo_sources, "_safe_open", _raise_500)
        with pytest.raises(urllib.error.HTTPError):
            photo_sources._fetch_hexdb("aabbcc")

    def test_icao_hex_is_percent_encoded(self, monkeypatch):
        captured = []
        def fake_open(url, *, timeout, max_bytes):
            captured.append(url)
            return b"n/a", {}
        monkeypatch.setattr(photo_sources, "_safe_open", fake_open)
        photo_sources._fetch_hexdb("../etc/passwd")
        assert "%2E%2E%2Fetc%2Fpasswd" in captured[0].upper() or "..%2Fetc%2Fpasswd" in captured[0]


# ---------------------------------------------------------------------------
# fetch_photo — chain behaviour
# ---------------------------------------------------------------------------

class TestFetchPhoto:
    def test_planespotters_hit_returns_immediately(self, monkeypatch):
        calls = []
        ps_result = PhotoResult(thumbnail_url="https://ps.com/t.jpg")
        monkeypatch.setattr(photo_sources, "SOURCES", [
            lambda h: (calls.append("ps") or ps_result),
            lambda h: (calls.append("ad") or None),
            lambda h: (calls.append("hx") or None),
        ])
        result = photo_sources.fetch_photo("aabbcc")
        assert result is ps_result
        assert calls == ["ps"]

    def test_falls_through_to_airport_data(self, monkeypatch):
        ad_result = PhotoResult(thumbnail_url="https://ad.com/t.jpg")
        monkeypatch.setattr(photo_sources, "SOURCES", [
            lambda h: None,
            lambda h: ad_result,
            lambda h: None,
        ])
        assert photo_sources.fetch_photo("aabbcc") is ad_result

    def test_falls_through_to_hexdb(self, monkeypatch):
        hx_result = PhotoResult(thumbnail_url="https://hexdb.io/img.jpg")
        monkeypatch.setattr(photo_sources, "SOURCES", [
            lambda h: None,
            lambda h: None,
            lambda h: hx_result,
        ])
        assert photo_sources.fetch_photo("aabbcc") is hx_result

    def test_all_fail_returns_none(self, monkeypatch):
        monkeypatch.setattr(photo_sources, "SOURCES", [
            lambda h: None, lambda h: None, lambda h: None,
        ])
        assert photo_sources.fetch_photo("aabbcc") is None

    def test_source_exception_skips_to_next(self, monkeypatch):
        ad_result = PhotoResult(thumbnail_url="https://ad.com/t.jpg")
        def boom(h): raise OSError("network")
        monkeypatch.setattr(photo_sources, "SOURCES", [
            boom, lambda h: ad_result, lambda h: None,
        ])
        assert photo_sources.fetch_photo("aabbcc") is ad_result

    def test_all_raise_returns_none(self, monkeypatch):
        def boom(h): raise OSError("network")
        monkeypatch.setattr(photo_sources, "SOURCES", [boom, boom, boom])
        assert photo_sources.fetch_photo("aabbcc") is None

    def test_sources_called_with_icao_hex(self, monkeypatch):
        received = []
        monkeypatch.setattr(photo_sources, "SOURCES", [
            lambda h: received.append(h) or None,
        ])
        photo_sources.fetch_photo("abc123")
        assert received == ["abc123"]


# ---------------------------------------------------------------------------
# resolve_photo — shared lookup ladder
# ---------------------------------------------------------------------------

class TestResolvePhoto:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = database.connect(db_path)
        conn.executescript(database.DDL)
        database._migrate(conn)
        self.conn = conn
        yield
        conn.close()

    def _now(self) -> int:
        return int(time.time())

    def test_specific_cache_hit_returns_url(self):
        now = self._now()
        self.conn.execute(
            "INSERT INTO photos VALUES ('abc123', 'https://ps.com/t.jpg', NULL, NULL, NULL, ?)",
            (now,),
        )
        self.conn.commit()
        called = []
        result, is_type = photo_sources.resolve_photo(
            self.conn, "abc123", "B738",
            fetcher=lambda h: called.append(h) or None,
        )
        assert result["thumbnail_url"] == "https://ps.com/t.jpg"
        assert is_type is False
        assert called == []

    def test_specific_negative_cache_returns_none(self):
        now = self._now()
        self.conn.execute(
            "INSERT INTO photos VALUES ('abc123', NULL, NULL, NULL, NULL, ?)",
            (now,),
        )
        self.conn.commit()
        called = []
        result, is_type = photo_sources.resolve_photo(
            self.conn, "abc123", "B738",
            fetcher=lambda h: called.append(h) or None,
        )
        assert result is None and is_type is False
        assert called == []

    def test_type_cache_hit_returns_type_photo(self):
        now = self._now()
        self.conn.execute(
            "INSERT INTO type_photos VALUES ('B738', 'https://ps.com/b738.jpg', NULL, NULL, NULL, ?)",
            (now,),
        )
        self.conn.commit()
        result, is_type = photo_sources.resolve_photo(
            self.conn, "abc123", "B738", fetcher=lambda h: None,
        )
        assert result["thumbnail_url"] == "https://ps.com/b738.jpg"
        assert is_type is True

    def test_join_finds_cached_photo_for_type(self):
        now = self._now()
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('aabbcc', 'G-ABCD', 'B738', 'Boeing 737-800', 0)"
        )
        self.conn.execute(
            "INSERT INTO photos VALUES ('aabbcc', 'https://ps.com/other.jpg', NULL, NULL, NULL, ?)",
            (now,),
        )
        self.conn.commit()
        called = []
        result, is_type = photo_sources.resolve_photo(
            self.conn, "abc123", "B738",
            fetcher=lambda h: called.append(h) or None,
        )
        assert result["thumbnail_url"] == "https://ps.com/other.jpg"
        assert is_type is True
        assert called == []
        # And the JOIN result was promoted to type_photos cache
        row = self.conn.execute(
            "SELECT thumbnail_url FROM type_photos WHERE type_code='B738'"
        ).fetchone()
        assert row and row[0] == "https://ps.com/other.jpg"

    def test_specific_fetch_succeeds(self):
        result, is_type = photo_sources.resolve_photo(
            self.conn, "abc123", None,
            fetcher=lambda h: PhotoResult(thumbnail_url="https://ps.com/sp.jpg",
                                          large_url="https://ps.com/l.jpg"),
        )
        assert result["thumbnail_url"] == "https://ps.com/sp.jpg"
        assert result["large_url"] == "https://ps.com/l.jpg"
        assert is_type is False
        # cached
        row = self.conn.execute(
            "SELECT thumbnail_url FROM photos WHERE icao_hex='abc123'"
        ).fetchone()
        assert row[0] == "https://ps.com/sp.jpg"

    def test_probe_succeeds_when_specific_fails(self):
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('probe01', 'G-PRB', 'EF2K', 'Eurofighter Typhoon', 1)"
        )
        self.conn.commit()
        seen = []
        def fake_fetch(h):
            seen.append(h)
            return PhotoResult(thumbnail_url="https://ps.com/ef2k.jpg") if h == "probe01" else None
        result, is_type = photo_sources.resolve_photo(
            self.conn, "abc123", "EF2K", fetcher=fake_fetch,
        )
        assert result["thumbnail_url"] == "https://ps.com/ef2k.jpg"
        assert is_type is True
        assert seen == ["abc123", "probe01"]
        # Both specific (probe) and type cached
        row = self.conn.execute(
            "SELECT thumbnail_url FROM type_photos WHERE type_code='EF2K'"
        ).fetchone()
        assert row[0] == "https://ps.com/ef2k.jpg"

    def test_all_fail_caches_negatives(self):
        self.conn.execute(
            "INSERT INTO aircraft_db (icao_hex, registration, type_code, type_desc, flags) "
            "VALUES ('probe01', 'G-PRB', 'EF2K', 'Eurofighter Typhoon', 1)"
        )
        self.conn.commit()
        result, is_type = photo_sources.resolve_photo(
            self.conn, "abc123", "EF2K", fetcher=lambda h: None,
        )
        assert result is None and is_type is False
        p_row = self.conn.execute(
            "SELECT thumbnail_url FROM photos WHERE icao_hex='abc123'"
        ).fetchone()
        t_row = self.conn.execute(
            "SELECT thumbnail_url FROM type_photos WHERE type_code='EF2K'"
        ).fetchone()
        assert p_row[0] is None
        assert t_row[0] is None

    def test_no_type_code_skips_type_paths(self):
        result, is_type = photo_sources.resolve_photo(
            self.conn, "abc123", None, fetcher=lambda h: None,
        )
        assert result is None and is_type is False
        # No type_photos row should be written when type_code is None
        assert self.conn.execute("SELECT COUNT(*) FROM type_photos").fetchone()[0] == 0
