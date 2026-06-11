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
