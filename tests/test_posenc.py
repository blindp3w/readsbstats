"""posenc — scaled-integer codecs for schema-v6 positions rows."""
import pytest

from readsbstats import posenc


class TestCoordCodec:
    def test_round_trip(self):
        assert posenc.dec5(posenc.enc5(52.20491)) == pytest.approx(52.20491)

    def test_none_passthrough(self):
        assert posenc.enc5(None) is None
        assert posenc.dec5(None) is None
        assert posenc.enc1(None) is None
        assert posenc.dec1(None) is None

    def test_negative_coords(self):
        assert posenc.enc5(-21.00001) == -2100001

    def test_deci_codec(self):
        assert posenc.enc1(437.5) == 4375
        assert posenc.dec1(-235) == -23.5   # rssi is negative dB


class TestOverflowGuard:
    """enc5/enc1 must return None instead of an astronomically large int that
    would overflow SQLite's 64-bit signed INTEGER column (schema v6)."""

    _INT64_MAX = 9223372036854775807
    _INT64_MIN = -9223372036854775808

    def test_enc1_huge_positive_returns_none(self):
        assert posenc.enc1(1e300) is None

    def test_enc1_huge_negative_returns_none(self):
        assert posenc.enc1(-1e300) is None

    def test_enc5_huge_positive_returns_none(self):
        assert posenc.enc5(1e300) is None

    def test_enc5_huge_negative_returns_none(self):
        assert posenc.enc5(-1e300) is None

    def test_enc1_normal_values_unaffected(self):
        """Normal readsb track/gs/rssi values must still encode correctly."""
        assert posenc.enc1(359.9) == 3599
        assert posenc.enc1(-50.5) == -505
        assert posenc.enc1(0.0) == 0

    def test_enc5_normal_values_unaffected(self):
        """Normal lat/lon values must still encode correctly."""
        assert posenc.enc5(52.20491) == 5220491
        assert posenc.enc5(-21.00001) == -2100001

    def test_enc1_large_but_in_range_passes_through(self):
        """A large-but-finite value whose encoded form fits in INT64 must pass."""
        # 1e17 * 10 = 1e18, well within INT64_MAX (~9.2e18)
        assert posenc.enc1(1e17) == 1_000_000_000_000_000_000

    def test_enc5_large_but_in_range_passes_through(self):
        # 1e13 * 100000 = 1e18, well within INT64_MAX
        assert posenc.enc5(1e13) == 1_000_000_000_000_000_000

    def test_enc5_straddles_int64_boundary(self):
        # _INT64_MAX ≈ 9.2233720e18, so the enc5 input boundary is ≈9.2233720e13.
        # 9.22e13 ×1e5 = 9.22e18 fits; 9.23e13 ×1e5 = 9.23e18 overflows. The
        # 9.22/9.23 gap (~1e16) dwarfs float ULP at this magnitude, so the
        # straddle is deterministic.
        assert posenc.enc5(9.22e13) is not None
        assert posenc.enc5(9.23e13) is None
        assert posenc.enc5(-9.22e13) is not None
        assert posenc.enc5(-9.23e13) is None

    def test_enc1_straddles_int64_boundary(self):
        # ×10 codec → input boundary ≈9.2233720e17.
        assert posenc.enc1(9.22e17) is not None
        assert posenc.enc1(9.23e17) is None
        assert posenc.enc1(-9.22e17) is not None
        assert posenc.enc1(-9.23e17) is None


class TestSourceCodec:
    def test_known_round_trip(self):
        for name, code in posenc.SOURCE_TO_CODE.items():
            assert posenc.CODE_TO_SOURCE[code] == name

    def test_is_adsb_contract_covered(self):
        """collector._is_adsb/_is_mlat classify these five types — they must
        round-trip losslessly or _purge's aggregate recompute reclassifies."""
        for name in ("adsb_icao", "adsb_icao_nt", "adsr_icao", "adsc", "mlat"):
            assert name in posenc.SOURCE_TO_CODE

    def test_unknown_string_maps_to_other(self):
        assert posenc.encode_source("weird_new_type") == posenc.OTHER_CODE
        assert posenc.decode_source(posenc.OTHER_CODE) == "other"

    def test_none_source(self):
        assert posenc.encode_source(None) is None
        assert posenc.decode_source(None) is None

    def test_sql_case_translates_and_keeps_null(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        case = posenc.sql_source_case("st")
        assert conn.execute(
            f"SELECT {case} FROM (SELECT 'mlat' AS st)").fetchone()[0] == 1
        assert conn.execute(
            f"SELECT {case} FROM (SELECT NULL AS st)").fetchone()[0] is None
        assert conn.execute(
            f"SELECT {case} FROM (SELECT 'garbage' AS st)").fetchone()[0] == posenc.OTHER_CODE
