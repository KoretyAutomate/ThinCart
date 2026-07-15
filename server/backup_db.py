"""
backup_db.py — guarded SQLite backup / checkpoint helper (stdlib only).

Used by four call sites against three different databases, so --db is REQUIRED
(a config-default resolution here once nearly checkpointed the wrong DB while
exiting 0, stranding freshly imported rows in a -wal the upload omitted):

  nightly on-machine backup ... python3 /srv/server/backup_db.py --db /data/thincart.db
  E4 swap checkpoint .......... python3 /srv/server/backup_db.py --db /data/thincart.db --checkpoint
  E1 dry-run finalize ......... python3 server/backup_db.py --db <fresh-import.db> --checkpoint
  step-19 DGX final snapshot .. python3 server/backup_db.py --db ~/.../thincart.db

Default mode: `Connection.backup()` to a UTC-timestamped artifact next to the
DB (backup-YYYY-MM-DDTHHMMZ.db — minute granularity so a freshness check is
well-defined), verify it with PRAGMA integrity_check, prune old artifacts
(keep newest KEEP), and print `ARTIFACT:<filename>` as the LAST stdout line —
the pinned protocol the DGX-side fetch greps for (remote exit codes over
`fly ssh console -C` are not trusted; the artifact name + its stamp are).

--checkpoint mode: PRAGMA wal_checkpoint(TRUNCATE) and exit non-zero unless
the result row reports busy=0 (a bare CLI checkpoint exits 0 even when a
lingering reader blocks it — which is exactly when data would be lost).
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

KEEP = 7  # on-machine artifacts; the DGX side keeps its own 14


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def checkpoint(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        for attempt in range(5):
            busy, log_frames, ckpt_frames = conn.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy == 0:
                print(f"checkpoint clean (log={log_frames}, checkpointed={ckpt_frames})")
                return
            print(f"checkpoint busy (attempt {attempt + 1}/5) — a reader is pinning the WAL")
            time.sleep(1.0)
        fail("wal_checkpoint(TRUNCATE) still busy after 5 attempts — writes would be stranded")
    finally:
        conn.close()


def backup(db: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%MZ")
    artifact = db.parent / f"backup-{stamp}.db"
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(artifact)
    try:
        src.backup(dst)  # consistent even against a live WAL writer
        dst.commit()
    finally:
        src.close()
        dst.close()
    check = sqlite3.connect(f"file:{artifact}?mode=ro", uri=True)
    try:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok":
        artifact.unlink(missing_ok=True)
        fail(f"integrity_check failed on artifact: {ok}")
    if artifact.stat().st_size == 0:
        artifact.unlink(missing_ok=True)
        fail("artifact is empty")
    for old in sorted(db.parent.glob("backup-*.db"))[:-KEEP]:
        old.unlink()
    # pinned protocol line — MUST be last on stdout; absence = failure upstream
    print(f"ARTIFACT:{artifact.name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path,
                    help="path to the SQLite database (REQUIRED — no default on purpose)")
    ap.add_argument("--checkpoint", action="store_true",
                    help="wal_checkpoint(TRUNCATE) with busy=0 verification instead of a backup")
    args = ap.parse_args()
    if not args.db.exists():
        fail(f"no such database: {args.db}")
    if args.checkpoint:
        checkpoint(args.db)
    else:
        backup(args.db)


if __name__ == "__main__":
    main()
