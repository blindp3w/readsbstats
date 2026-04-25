"""
Concurrent write/read tests for WAL-mode SQLite.

Verifies that the collector (writer) and web server (reader) can operate
on the same database file simultaneously without "database is locked" errors.
"""

import os
import sqlite3
import tempfile
import threading
import time

import pytest

from readsbstats import database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _connect(path: str) -> sqlite3.Connection:
    """Open a WAL-mode connection to the given file."""
    return database.connect(path)


def _seed(conn: sqlite3.Connection) -> int:
    """Create schema, insert one flight, return its id."""
    conn.executescript(database.DDL)
    database._migrate(conn)
    cur = conn.execute(
        "INSERT INTO flights (icao_hex, first_seen, last_seen, total_positions) "
        "VALUES ('aabbcc', 1000000, 1003600, 0)"
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConcurrentWriteRead:
    """Writer inserts positions while reader queries flights — no lock errors."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        yield
        # Clean up
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + ext)
            except FileNotFoundError:
                pass
        os.rmdir(self.tmpdir)

    def test_writer_and_reader_no_lock_errors(self):
        """Concurrent writer + reader on WAL DB must not raise 'database is locked'."""
        writer_conn = _connect(self.db_path)
        flight_id = _seed(writer_conn)

        errors: list[Exception] = []
        stop = threading.Event()
        writes_done = 0
        reads_done = 0

        def writer():
            nonlocal writes_done
            try:
                for i in range(200):
                    if stop.is_set():
                        break
                    with writer_conn:
                        writer_conn.execute(
                            "INSERT INTO positions (flight_id, ts, lat, lon) "
                            "VALUES (?, ?, ?, ?)",
                            (flight_id, 1000000 + i, 52.0 + i * 0.001, 21.0),
                        )
                        writer_conn.execute(
                            "UPDATE flights SET total_positions = total_positions + 1, "
                            "last_seen = ? WHERE id = ?",
                            (1000000 + i, flight_id),
                        )
                    writes_done += 1
            except Exception as exc:
                errors.append(exc)
                stop.set()

        def reader():
            nonlocal reads_done
            reader_conn = _connect(self.db_path)
            try:
                while not stop.is_set() and reads_done < 200:
                    reader_conn.execute(
                        "SELECT f.*, COUNT(p.rowid) AS pos_count "
                        "FROM flights f "
                        "LEFT JOIN positions p ON p.flight_id = f.id "
                        "WHERE f.id = ? "
                        "GROUP BY f.id",
                        (flight_id,),
                    ).fetchone()
                    reads_done += 1
            except Exception as exc:
                errors.append(exc)
                stop.set()
            finally:
                reader_conn.close()

        t_writer = threading.Thread(target=writer)
        t_reader = threading.Thread(target=reader)
        t_writer.start()
        t_reader.start()
        t_writer.join(timeout=10)
        stop.set()
        t_reader.join(timeout=10)
        writer_conn.close()

        assert not errors, f"Concurrent access errors: {errors}"
        assert writes_done > 0, "Writer thread did not execute"
        assert reads_done > 0, "Reader thread did not execute"

    def test_two_writers_serialized_via_wal(self):
        """Two writers with busy_timeout should both succeed (WAL serializes writes)."""
        conn1 = _connect(self.db_path)
        flight_id = _seed(conn1)
        conn2 = _connect(self.db_path)
        # Ensure both have busy_timeout so they wait instead of failing immediately
        conn1.execute("PRAGMA busy_timeout = 5000")
        conn2.execute("PRAGMA busy_timeout = 5000")

        errors: list[Exception] = []
        counts = {"conn1": 0, "conn2": 0}

        def insert_positions(conn, key, start):
            try:
                for i in range(100):
                    with conn:
                        conn.execute(
                            "INSERT INTO positions (flight_id, ts, lat, lon) "
                            "VALUES (?, ?, ?, ?)",
                            (flight_id, start + i, 52.0 + i * 0.001, 21.0),
                        )
                    counts[key] += 1
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=insert_positions, args=(conn1, "conn1", 2000000))
        t2 = threading.Thread(target=insert_positions, args=(conn2, "conn2", 3000000))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        conn1.close()
        conn2.close()

        assert not errors, f"Dual-writer errors: {errors}"
        assert counts["conn1"] == 100
        assert counts["conn2"] == 100

        # Verify all rows landed
        verify = _connect(self.db_path)
        total = verify.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        verify.close()
        assert total == 200

    def test_reader_sees_committed_writes(self):
        """Reader connection sees rows after writer commits (WAL visibility)."""
        writer_conn = _connect(self.db_path)
        flight_id = _seed(writer_conn)
        reader_conn = _connect(self.db_path)

        # Reader sees zero positions initially
        count = reader_conn.execute(
            "SELECT COUNT(*) FROM positions WHERE flight_id = ?", (flight_id,)
        ).fetchone()[0]
        assert count == 0

        # Writer inserts and commits
        with writer_conn:
            for i in range(10):
                writer_conn.execute(
                    "INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?, ?, 52.0, 21.0)",
                    (flight_id, 1000000 + i),
                )

        # Reader sees the committed rows
        count = reader_conn.execute(
            "SELECT COUNT(*) FROM positions WHERE flight_id = ?", (flight_id,)
        ).fetchone()[0]
        assert count == 10

        writer_conn.close()
        reader_conn.close()

    def test_reader_not_blocked_by_long_write_transaction(self):
        """Reader can query while writer holds an open transaction."""
        writer_conn = _connect(self.db_path)
        flight_id = _seed(writer_conn)
        reader_conn = _connect(self.db_path)

        barrier = threading.Barrier(2, timeout=5)
        reader_result = {}
        errors: list[Exception] = []

        def long_writer():
            try:
                writer_conn.execute("BEGIN IMMEDIATE")
                for i in range(50):
                    writer_conn.execute(
                        "INSERT INTO positions (flight_id, ts, lat, lon) VALUES (?, ?, 52.0, 21.0)",
                        (flight_id, 1000000 + i),
                    )
                # Signal reader to query while transaction is open
                barrier.wait()
                # Hold transaction open briefly
                time.sleep(0.05)
                writer_conn.execute("COMMIT")
            except Exception as exc:
                errors.append(exc)

        def concurrent_reader():
            try:
                barrier.wait()
                # Query while writer transaction is open — should succeed in WAL mode
                row = reader_conn.execute(
                    "SELECT * FROM flights WHERE id = ?", (flight_id,)
                ).fetchone()
                reader_result["found"] = row is not None
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=long_writer)
        t2 = threading.Thread(target=concurrent_reader)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        writer_conn.close()
        reader_conn.close()

        assert not errors, f"Errors during concurrent access: {errors}"
        assert reader_result.get("found") is True, "Reader was blocked or failed"
