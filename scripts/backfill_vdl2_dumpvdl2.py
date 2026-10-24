#!/usr/bin/env python3
"""One-shot backfill: dumpvdl2 sublabel/mfi, XID position/destination, ATN CM logon.

Rows ingested before the dumpvdl2 sublabel/XID fix have NULL ``sublabel``,
``mfi``, ``lat``, ``lon``, ``alt`` and ``dsta`` even though their verbatim ``raw``
JSON carries the data (H1 sublabel/MFI, XID ``ac_location`` / ``dst_airport``).
This re-runs the normalizer over those rows and fills the NULL columns. It only
ever fills NULLs — an existing non-NULL value is never overwritten.

It also lifts ATN Context-Management logon rows (``context_mgmt`` in ``raw``), which
pre-2.27.0 ingest stored content-free: ``label='CM'``, ``body='LOGON <flight>
<dep>-<dest>'`` and ``dsta`` = the destination. Filling ``body`` fires the FTS
update trigger, so the new bodies become searchable. Re-run once after upgrading to
2.27.0 (safe on a DB already backfilled for sublabel/XID: those rows no-op).

Safe to run while ``readsbstats-vdl2`` is live (do NOT stop any service): the
connection is WAL with a 30 s busy_timeout and each chunk is its own short
transaction. Only needed once, after deploying the fix; idempotent (a second run
reports ``updated: 0``). /etc/readsbstats/vdl2.env is not loaded under sudo, so
pass ``--db`` explicitly:

    sudo -u readsbstats /opt/readsbstats/venv/bin/python \\
        /opt/readsbstats/scripts/backfill_vdl2_dumpvdl2.py \\
        --db /mnt/ext/readsbstats/vdl2.db

``--dry-run`` reports what would change without writing (and without running
the schema migration).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from readsbstats import config
from readsbstats.vdl2 import db as vdl2_db
from readsbstats.vdl2 import normalize

_COLS = ("sublabel", "mfi", "lat", "lon", "alt", "dsta", "label", "body")


def backfill(path: str, *, chunk: int = 2000, dry_run: bool = False) -> dict:
    conn = vdl2_db.connect(path)
    try:
        if not dry_run:
            # Adds sublabel/mfi if missing and swaps the FTS update trigger for the
            # body-only form, so these non-body UPDATEs skip FTS reindexing.
            vdl2_db.migrate(conn, build_fts=True)
        have = {r[1] for r in conn.execute("PRAGMA table_info(vdl2_messages)")}
        # A dry run never ALTERs: columns not yet present are treated as NULL.
        sel = ", ".join(c if c in have else f"NULL AS {c}" for c in _COLS)
        sql = (
            f"SELECT id, raw, {sel} FROM vdl2_messages "
            "WHERE decoder = 'dumpvdl2' AND id > ? "
            "AND (raw LIKE '%\"sublabel\":%' OR raw LIKE '%\"xid\":{%' "
            "OR instr(raw, 'context_mgmt') > 0) "
            "ORDER BY id LIMIT ?"
        )
        res = {"scanned": 0, "updated": 0, "bad_json": 0,
               "filled": {c: 0 for c in _COLS}}
        last_id = 0
        t0 = time.monotonic()
        while True:
            rows = conn.execute(sql, (last_id, chunk)).fetchall()
            if not rows:
                break
            for r in rows:
                last_id = r["id"]
                res["scanned"] += 1
                try:
                    obj = json.loads(r["raw"])
                except (ValueError, TypeError):
                    res["bad_json"] += 1
                    continue
                rec = normalize.normalize(obj, "dumpvdl2")
                if rec is None:
                    continue
                # Fill NULLs only: COALESCE(current, new) never overwrites a value.
                changed = {c: rec.get(c) for c in _COLS
                           if r[c] is None and rec.get(c) is not None}
                if not changed:
                    continue
                res["updated"] += 1
                for c in changed:
                    res["filled"][c] += 1
                if not dry_run:
                    sets = ", ".join(f"{c} = ?" for c in changed)
                    conn.execute(f"UPDATE vdl2_messages SET {sets} WHERE id = ?",
                                 (*changed.values(), r["id"]))
            conn.commit()   # short transaction per chunk — never blocks the live writer long
            print(f"  scanned {res['scanned']:,}  updated {res['updated']:,}  "
                  f"(id {last_id}, {time.monotonic() - t0:.0f}s)", flush=True)
        return res
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", default=config.VDL2_DB_PATH,
                    help="path to vdl2.db (default: RSBS_VDL2_DB_PATH)")
    ap.add_argument("--chunk", type=int, default=2000,
                    help="rows per transaction (default 2000)")
    ap.add_argument("--dry-run", action="store_true",
                    help="count what would change; write nothing")
    args = ap.parse_args()
    # vdl2_db.connect() would silently create a missing file; reject up front.
    if not os.path.exists(args.db):
        print(f"error: database not found: {args.db}", file=sys.stderr)
        sys.exit(1)
    if args.chunk < 1:
        ap.error("--chunk must be >= 1")
    res = backfill(args.db, chunk=args.chunk, dry_run=args.dry_run)
    print(("dry run: " if args.dry_run else "") + json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
