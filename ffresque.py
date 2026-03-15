#!/usr/bin/env python3
"""ffresque — block-level file rescue from damaged media.

Copies files block-by-block, skipping unreadable blocks and recording
their status in a SQLite database. On re-run (possibly from a different
copy of the same data, e.g. the other disk of a broken mirror), only
retries blocks previously marked as bad.
"""

import argparse
import collections
import math
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone


# ── Database ────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    file TEXT NOT NULL,
    block_num INTEGER NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (file, block_num)
);
CREATE INDEX IF NOT EXISTS idx_file_status ON blocks(file, status);

CREATE TABLE IF NOT EXISTS files (
    file TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    total_blocks INTEGER NOT NULL,
    ok_blocks INTEGER DEFAULT 0,
    bad_blocks INTEGER DEFAULT 0,
    complete BOOLEAN DEFAULT 0
);
"""


def open_db(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def get_block_statuses(conn, rel_path):
    """Return dict {block_num: status} for a file."""
    rows = conn.execute(
        "SELECT block_num, status FROM blocks WHERE file = ?", (rel_path,)
    )
    return {r[0]: r[1] for r in rows}


def upsert_block(conn, rel_path, block_num, status):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO blocks (file, block_num, status, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(file, block_num) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
        (rel_path, block_num, status, now),
    )


def upsert_file(conn, rel_path, size, total_blocks, ok_blocks, bad_blocks):
    complete = 1 if bad_blocks == 0 and ok_blocks == total_blocks else 0
    conn.execute(
        "INSERT INTO files (file, size, total_blocks, ok_blocks, bad_blocks, complete) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(file) DO UPDATE SET "
        "size=excluded.size, total_blocks=excluded.total_blocks, "
        "ok_blocks=excluded.ok_blocks, bad_blocks=excluded.bad_blocks, "
        "complete=excluded.complete",
        (rel_path, size, total_blocks, ok_blocks, bad_blocks, complete),
    )
    return complete == 1


# ── Copy logic ──────────────────────────────────────────────────────

def move_to_dst(rel_path, work_dir, dst_dir):
    """Move a fully recovered file from work_dir to dst_dir."""
    work_path = os.path.join(work_dir, rel_path)
    dst_path = os.path.join(dst_dir, rel_path)
    if not os.path.exists(work_path):
        return
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    shutil.move(work_path, dst_path)


def process_file(rel_path, src_dir, work_dir, dst_dir, block_size, conn,
                 skip_existing=True, skip_bad_blocks=False):
    """Process a single file. Returns (blocks_ok_session, blocks_bad, newly_complete, skipped)."""
    src_path = os.path.join(src_dir, rel_path)
    dst_path = os.path.join(dst_dir, rel_path)
    work_path = os.path.join(work_dir, rel_path)

    # If already moved to dst in a previous session, skip
    if skip_existing and os.path.exists(dst_path):
        return 0, 0, True, True

    # stat source
    try:
        st = os.stat(src_path)
    except FileNotFoundError:
        print(f"  SKIP (not found): {rel_path}", file=sys.stderr)
        return 0, 0, False, True

    size = st.st_size
    if size == 0:
        # empty file — move straight to dst
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        open(dst_path, "ab").close()
        upsert_file(conn, rel_path, 0, 0, 0, 0)
        return 0, 0, True, False

    total_blocks = math.ceil(size / block_size)
    existing = get_block_statuses(conn, rel_path)

    # Determine which blocks to try
    if existing:
        # Skip if all blocks have been attempted and flag is set
        if skip_bad_blocks and len(existing) >= total_blocks:
            return 0, 0, False, True
        blocks_to_try = [b for b in range(total_blocks) if existing.get(b) != "ok"]
    else:
        blocks_to_try = list(range(total_blocks))

    if not blocks_to_try:
        # All blocks already ok — move if still in work_dir
        ok_count = total_blocks
        upsert_file(conn, rel_path, size, total_blocks, ok_count, 0)
        move_to_dst(rel_path, work_dir, dst_dir)
        return 0, 0, True, True

    # Ensure work directory exists
    os.makedirs(os.path.dirname(work_path), exist_ok=True)

    # Open source (read-only) and work file
    fd_src = os.open(src_path, os.O_RDONLY)
    try:
        # Create/open work file and ensure correct size
        if not os.path.exists(work_path):
            fd_dst = os.open(work_path, os.O_CREAT | os.O_WRONLY, 0o644)
            os.ftruncate(fd_dst, size)
        else:
            fd_dst = os.open(work_path, os.O_WRONLY)
            cur_size = os.fstat(fd_dst).st_size
            if cur_size != size:
                os.ftruncate(fd_dst, size)

        try:
            session_ok = 0
            for bnum in blocks_to_try:
                offset = bnum * block_size
                to_read = min(block_size, size - offset)

                try:
                    data = os.pread(fd_src, to_read, offset)
                    if len(data) < to_read:
                        # Short read — pad with zeros
                        data = data + b"\x00" * (to_read - len(data))
                    os.pwrite(fd_dst, data, offset)
                    upsert_block(conn, rel_path, bnum, "ok")
                    session_ok += 1
                except OSError:
                    # I/O error — write zeros and mark bad
                    os.pwrite(fd_dst, b"\x00" * to_read, offset)
                    upsert_block(conn, rel_path, bnum, "bad")
        finally:
            os.close(fd_dst)
    finally:
        os.close(fd_src)

    # Recount from DB
    row = conn.execute(
        "SELECT "
        "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN status='bad' THEN 1 ELSE 0 END) "
        "FROM blocks WHERE file = ?",
        (rel_path,),
    ).fetchone()
    ok_count = row[0] or 0
    bad_count = row[1] or 0
    complete = upsert_file(conn, rel_path, size, total_blocks, ok_count, bad_count)

    # If fully recovered, move from work to dst
    if complete:
        move_to_dst(rel_path, work_dir, dst_dir)

    return session_ok, bad_count, complete, False


def human_size(nbytes):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PiB"


def format_duration(seconds):
    """Format seconds into a human-readable string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


# ── Commands ────────────────────────────────────────────────────────

def scan_src(src_dir):
    """Walk src_dir and return sorted list of relative file paths."""
    result = []
    for dirpath, _dirnames, filenames in os.walk(src_dir):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            result.append(os.path.relpath(full, src_dir))
    result.sort()
    return result


def cmd_copy(args):
    # Build file list
    if args.bad_files:
        with open(args.bad_files) as f:
            files = [line.strip() for line in f if line.strip()]
    else:
        print(f"Scanning {args.src} ...", file=sys.stderr)
        files = scan_src(args.src)

    if not files:
        print("No files to process.")
        return

    conn = open_db(args.db)
    block_size = args.block_size
    total_files = len(files)

    # Track files that were complete before this session
    prev_complete = set()
    for row in conn.execute("SELECT file FROM files WHERE complete = 1"):
        prev_complete.add(row[0])

    # Count how many files need work (not yet complete)
    prev_complete_in_list = sum(1 for f in files if f in prev_complete)
    files_to_process = total_files - prev_complete_in_list

    # Pre-session stats from DB
    row = conn.execute(
        "SELECT "
        "COALESCE(SUM(ok_blocks), 0), "
        "COALESCE(SUM(bad_blocks), 0) "
        "FROM files"
    ).fetchone()
    prev_ok_blocks = row[0]
    prev_bad_blocks = row[1]

    print(f"=== Starting ===")
    print(f"Files in list: {total_files}")
    print(f"Already complete: {prev_complete_in_list}")
    print(f"To process: {files_to_process}")
    if prev_bad_blocks > 0:
        skip_note = " (will be skipped due to --skip-bad-blocks)" if args.skip_bad_blocks else ""
        print(f"Bad blocks to retry: {prev_bad_blocks} ({human_size(prev_bad_blocks * block_size)}){skip_note}")
    print()

    session_ok_blocks = 0
    session_bad_blocks = 0
    session_new_complete = []
    commit_interval = 100
    is_tty = sys.stderr.isatty()
    # Sliding window for ETA: track (timestamp, file_index) at each checkpoint
    eta_window = collections.deque(maxlen=10)

    t0 = time.monotonic()
    last_progress_time = t0

    work_dir = args.work_dir
    dst_dir = args.dst
    skip_existing = args.skip_existing
    skip_bad_blocks = args.skip_bad_blocks
    session_skipped = 0

    interrupted = False
    try:
        for i, rel_path in enumerate(files, 1):
            ok, bad, complete, skipped = process_file(
                rel_path, args.src, work_dir, dst_dir, block_size, conn,
                skip_existing=skip_existing, skip_bad_blocks=skip_bad_blocks,
            )
            session_ok_blocks += ok
            session_bad_blocks += bad
            if skipped:
                session_skipped += 1
            if complete and rel_path not in prev_complete:
                session_new_complete.append(rel_path)

            now = time.monotonic()
            time_to_report = now - last_progress_time >= 30
            if i % commit_interval == 0 or time_to_report:
                conn.commit()
                elapsed = now - t0
                pct = 100.0 * i / total_files

                # ETA from sliding window
                eta_window.append((now, i))
                eta_str = ""
                if len(eta_window) >= 2 and i < total_files:
                    wt0, wi0 = eta_window[0]
                    dt = now - wt0
                    di = i - wi0
                    if dt > 0 and di > 0:
                        rate = di / dt  # files per second
                        eta = (total_files - i) / rate
                        eta_str = f", ETA {format_duration(eta)}"

                total_session = session_ok_blocks + session_bad_blocks
                bad_pct = f" ({100.0 * session_bad_blocks / total_session:.1f}%)" if total_session > 0 else ""
                line = (f"  [{i}/{total_files}] {pct:.0f}% | "
                        f"{format_duration(elapsed)}{eta_str} | "
                        f"+{session_ok_blocks} ok, +{session_bad_blocks} bad{bad_pct}, "
                        f"+{len(session_new_complete)} complete")
                if is_tty:
                    print(f"\r{line}\033[K", end="", file=sys.stderr)
                else:
                    print(line, file=sys.stderr)
                last_progress_time = now
    except KeyboardInterrupt:
        interrupted = True
        if is_tty:
            print("", file=sys.stderr)
        print(f"\nInterrupted while processing: {rel_path} — saving progress...", file=sys.stderr)

    if is_tty and not interrupted:
        print("", file=sys.stderr)  # newline after \r progress

    conn.commit()

    # Gather final stats from DB
    row = conn.execute(
        "SELECT "
        "COUNT(*), "
        "SUM(CASE WHEN complete=1 THEN 1 ELSE 0 END), "
        "SUM(ok_blocks), "
        "SUM(bad_blocks) "
        "FROM files"
    ).fetchone()
    db_total_files = row[0] or 0
    db_complete = row[1] or 0
    db_ok_blocks = row[2] or 0
    db_bad_blocks = row[3] or 0

    # Write done-file (append newly completed)
    if session_new_complete and args.done_file:
        with open(args.done_file, "a") as f:
            for p in session_new_complete:
                f.write(p + "\n")

    conn.close()

    elapsed = time.monotonic() - t0

    # Print summary
    label = "Interrupted" if interrupted else "Session Summary"
    print()
    print(f"=== {label} ({format_duration(elapsed)}) ===")
    print(f"Files in list: {total_files}")
    print(f"Skipped: {session_skipped}")
    print(f"Blocks read OK (this session): {session_ok_blocks} ({human_size(session_ok_blocks * block_size)})")
    print(f"Blocks still bad: {db_bad_blocks} ({human_size(db_bad_blocks * block_size)})")
    print(f"Files fully recovered: {db_complete}/{db_total_files}")
    print(f"Files with remaining bad blocks: {db_total_files - db_complete}")
    print(f"New files completed this session: {len(session_new_complete)}")
    if session_new_complete:
        print(f"  (moved to {dst_dir})")


def cmd_status(args):
    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    conn = open_db(args.db)

    row = conn.execute(
        "SELECT "
        "COUNT(*), "
        "SUM(CASE WHEN complete=1 THEN 1 ELSE 0 END), "
        "SUM(ok_blocks), "
        "SUM(bad_blocks) "
        "FROM files"
    ).fetchone()
    total_files = row[0] or 0
    complete = row[1] or 0
    ok_blocks = row[2] or 0
    bad_blocks = row[3] or 0

    block_size_row = conn.execute(
        "SELECT block_num, COUNT(*) FROM blocks GROUP BY file ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()

    print("=== Database Status ===")
    print(f"Total files: {total_files}")
    print(f"Fully recovered (complete): {complete}")
    print(f"With bad blocks: {total_files - complete}")
    print()

    # Top 10 worst files
    worst = conn.execute(
        "SELECT file, bad_blocks, total_blocks FROM files "
        "WHERE bad_blocks > 0 ORDER BY CAST(bad_blocks AS REAL) / total_blocks DESC LIMIT 10"
    ).fetchall()
    if worst:
        print("Top 10 worst files:")
        for file, bad, total in worst:
            pct = 100.0 * bad / total if total else 0
            print(f"  {file}: {bad}/{total} bad ({pct:.1f}%)")
        print()

    print("Totals:")
    # Guess block size from DB — use 131072 as default
    bs = 131072
    print(f"  OK blocks: {ok_blocks} ({human_size(ok_blocks * bs)})")
    print(f"  Bad blocks: {bad_blocks} ({human_size(bad_blocks * bs)})")
    if ok_blocks + bad_blocks > 0:
        rate = 100.0 * ok_blocks / (ok_blocks + bad_blocks)
        print(f"  Recovery rate: {rate:.1f}%")

    conn.close()


# ── CLI ─────────────────────────────────────────────────────────────

USAGE = """\
ffresque — block-level file rescue from damaged media.

  %(prog)s copy  --src DIR --work-dir DIR --dst DIR [options]
  %(prog)s status [--db blocks.db]

copy options:
  --src SRC               Source directory on damaged media
  --work-dir WORK_DIR     Working directory for incomplete files;
                          bad blocks are filled with zeros
  --dst DST               Final destination; files are moved here
                          when fully recovered
  --bad-files FILE        List of files to recover (relative to --src,
                          one per line). If omitted, all files in --src
  --block-size N          Block size in bytes (default: 131072 = 128K)
  --db FILE               SQLite database for block tracking
                          (default: blocks.db)
  --done-file FILE        Append fully recovered paths here
                          (default: done-files.txt)
  --skip-existing         Skip files already in --dst (default: on)
  --no-skip-existing      Force reprocessing of files in --dst
  --skip-bad-blocks        Skip files with all blocks already attempted
  --no-skip-bad-blocks     Retry bad blocks (default: on)

status options:
  --db FILE               SQLite database path (default: blocks.db)

examples:
  %(prog)s copy --src /mnt/damaged --work-dir /tmp/work --dst /mnt/recovered
  %(prog)s copy --src /mnt/disk-b --work-dir /tmp/work --dst /mnt/recovered --db blocks.db
  %(prog)s status --db blocks.db
"""


def main():
    parser = argparse.ArgumentParser(
        usage=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # copy
    p_copy = sub.add_parser("copy", help="Copy files block-by-block")
    p_copy.add_argument("--src", required=True)
    p_copy.add_argument("--work-dir", required=True)
    p_copy.add_argument("--dst", required=True)
    p_copy.add_argument("--bad-files", default=None)
    p_copy.add_argument("--block-size", type=int, default=131072)
    p_copy.add_argument("--db", default="blocks.db")
    p_copy.add_argument("--done-file", default="done-files.txt")
    p_copy.add_argument("--skip-existing", action=argparse.BooleanOptionalAction,
                        default=True)
    p_copy.add_argument("--skip-bad-blocks", action=argparse.BooleanOptionalAction,
                        default=False)

    # status
    p_status = sub.add_parser("status", help="Show recovery status from DB")
    p_status.add_argument("--db", default="blocks.db")

    args = parser.parse_args()
    if args.command == "copy":
        cmd_copy(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
