#!/usr/bin/env python3
"""One-shot offline migration: schema v5 → v6 (slim positions).

Run with the collector AND web services STOPPED — update.sh detects
schema_version < 6 and runs this automatically during deploy:

    systemctl stop readsbstats-collector readsbstats-web
    sudo -u readsbstats /opt/readsbstats/venv/bin/python \\
        /opt/readsbstats/scripts/migrate_v6.py /mnt/ext/readsbstats/history.db

Rebuilds positions with scaled-INTEGER columns (posenc codecs), recreates
the two permanent indexes, stamps v6, ANALYZEs, then VACUUMs to reclaim
everything Phases 1-2 left on the freelist. ~10-20 min on the Pi for a
6M-row table. update.sh takes a VACUUM INTO backup beforehand.
"""
from __future__ import annotations

import argparse
import sys
import time

from readsbstats import database


def migrate(path: str) -> dict:
    conn = database.connect(path)
    try:
        ver = conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        ).fetchone()[0]
        if ver >= 6:
            print(f"already at schema v{ver}; nothing to do")
            return {"skipped": True}
        n = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        print(f"schema v{ver} → 6: rebuilding {n:,} positions rows …")
        t0 = time.monotonic()
        with conn:
            database.rebuild_positions_v6(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version "
                "VALUES (6, strftime('%s','now'))"
            )
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(f"foreign_key_check failed: {bad[:5]}")
        print(f"rebuild done in {time.monotonic() - t0:.0f}s; ANALYZE …")
        conn.execute("PRAGMA analysis_limit = 1000")
        conn.execute("ANALYZE")
        conn.commit()   # VACUUM refuses to run inside an open transaction
        print("VACUUM (reclaims Phase 1-3 freed pages — takes a while) …")
        t1 = time.monotonic()
        conn.execute("VACUUM")
        print(f"VACUUM done in {time.monotonic() - t1:.0f}s")
        # VACUUM in WAL mode leaves the rewritten pages in the WAL; truncate it
        # so the freed space is actually returned to the filesystem, not parked
        # in a large -wal alongside the slimmed main db.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"skipped": False, "rows": n}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("db_path")
    args = ap.parse_args()
    migrate(args.db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
