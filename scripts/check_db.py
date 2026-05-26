#!/usr/bin/env python3
"""SQLite integrity checker — for manual use and systemd timer invocation.

Exits 0 on clean check, 1 on corruption detected, 2 on open/query error.
Safe to run against a live WAL writer — opens read-only via URI.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

from readsbstats import config


def main() -> None:
    p = argparse.ArgumentParser(description="Check readsbstats DB integrity")
    p.add_argument("--db", default=config.DB_PATH, help="Path to history.db")
    p.add_argument(
        "--mode",
        choices=["quick", "full"],
        default="quick",
        help="quick=PRAGMA quick_check (default), full=PRAGMA integrity_check",
    )
    args = p.parse_args()

    try:
        # Path.as_uri() percent-encodes spaces, ?, #, %, etc. — direct
        # f-string concatenation would let those characters split the URI
        # into bogus query parameters and silently open a different file.
        db_uri = Path(args.db).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
        conn.execute("PRAGMA busy_timeout = 10000")
    except sqlite3.Error as exc:
        print(f"ERROR: cannot open {args.db}: {exc}", file=sys.stderr)
        sys.exit(2)

    pragma = "quick_check" if args.mode == "quick" else "integrity_check"
    print(f"Running PRAGMA {pragma} on {args.db} …")
    try:
        rows = conn.execute(f"PRAGMA {pragma}").fetchall()
    except sqlite3.Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        conn.close()
        sys.exit(2)
    conn.close()

    if len(rows) == 1 and rows[0][0] == "ok":
        print(f"OK — PRAGMA {pragma} passed")
        sys.exit(0)

    print(f"CORRUPTION DETECTED — {len(rows)} issue(s):", file=sys.stderr)
    for r in rows[:20]:
        print(f"  {r[0]}", file=sys.stderr)
    if len(rows) > 20:
        print(f"  … and {len(rows) - 20} more", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
