#!/usr/bin/env python3
"""fix_mtime — restore metadata (times, permissions, ownership) on recovered files.

Walks --dst and --work-dir, finds matching files in --src, and copies
metadata from source to destination. Useful for files recovered before
metadata preservation was added to ffresque.

Usage:
  fix_mtime.py --src /mnt/damaged --dst /mnt/recovered --work-dir /tmp/work
  fix_mtime.py --src /mnt/damaged --dst /mnt/recovered --dry-run
  fix_mtime.py --src /mnt/damaged --dst /mnt/recovered --no-owner
"""

import argparse
import os
import stat
import sys


def fix_tree(src_dir, target_dir, *, dry_run=False,
             times=True, perms=True, owner=True):
    """Sync metadata on files in target_dir from matching files in src_dir.

    Returns (fixed, skipped, missing) counts.
    """
    fixed = 0
    skipped = 0
    missing = 0

    for dirpath, _dirnames, filenames in os.walk(target_dir):
        for fname in filenames:
            target_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(target_path, target_dir)
            src_path = os.path.join(src_dir, rel_path)

            try:
                st_src = os.stat(src_path)
            except OSError:
                missing += 1
                continue

            st_dst = os.stat(target_path)

            needs_fix = False
            if times and st_dst.st_mtime != st_src.st_mtime:
                needs_fix = True
            if perms and stat.S_IMODE(st_dst.st_mode) != stat.S_IMODE(st_src.st_mode):
                needs_fix = True
            if owner and (st_dst.st_uid != st_src.st_uid or st_dst.st_gid != st_src.st_gid):
                needs_fix = True

            if not needs_fix:
                skipped += 1
                continue

            if dry_run:
                parts = []
                if times and st_dst.st_mtime != st_src.st_mtime:
                    parts.append("times")
                if perms and stat.S_IMODE(st_dst.st_mode) != stat.S_IMODE(st_src.st_mode):
                    parts.append(f"mode {oct(stat.S_IMODE(st_src.st_mode))}")
                if owner and (st_dst.st_uid != st_src.st_uid or st_dst.st_gid != st_src.st_gid):
                    parts.append(f"owner {st_src.st_uid}:{st_src.st_gid}")
                print(f"  would fix ({', '.join(parts)}): {rel_path}")
            else:
                if times:
                    os.utime(target_path, (st_src.st_atime, st_src.st_mtime))
                if perms:
                    try:
                        os.chmod(target_path, st_src.st_mode)
                    except OSError:
                        pass
                if owner:
                    try:
                        os.chown(target_path, st_src.st_uid, st_src.st_gid)
                    except OSError:
                        pass
            fixed += 1

    return fixed, skipped, missing


def main():
    parser = argparse.ArgumentParser(
        description="Restore metadata (times, permissions, ownership) on recovered files from source."
    )
    parser.add_argument("--src", required=True, help="Source directory (original files)")
    parser.add_argument("--dst", required=True, help="Destination directory (fully recovered)")
    parser.add_argument("--work-dir", default=None, help="Work directory (incomplete files)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be changed")
    parser.add_argument("--no-times", action="store_true", default=False,
                        help="Do not restore mtime/atime")
    parser.add_argument("--no-perms", action="store_true", default=False,
                        help="Do not restore file permissions (mode)")
    parser.add_argument("--no-owner", action="store_true", default=False,
                        help="Do not restore uid/gid ownership")

    args = parser.parse_args()

    if not os.path.isdir(args.src):
        print(f"Error: --src not found: {args.src}", file=sys.stderr)
        sys.exit(1)

    do_times = not args.no_times
    do_perms = not args.no_perms
    do_owner = not args.no_owner

    if not (do_times or do_perms or do_owner):
        print("Error: nothing to do (all --no-* flags set)", file=sys.stderr)
        sys.exit(1)

    total_fixed = 0
    total_skipped = 0
    total_missing = 0

    for label, path in [("dst", args.dst), ("work-dir", args.work_dir)]:
        if path is None:
            continue
        if not os.path.isdir(path):
            print(f"Warning: --{label} not found: {path}", file=sys.stderr)
            continue

        print(f"Processing {label}: {path}")
        fixed, skipped, missing = fix_tree(
            args.src, path, dry_run=args.dry_run,
            times=do_times, perms=do_perms, owner=do_owner,
        )
        print(f"  fixed: {fixed}, already ok: {skipped}, no source: {missing}")

        total_fixed += fixed
        total_skipped += skipped
        total_missing += missing

    prefix = "[dry-run] " if args.dry_run else ""
    print(f"\n{prefix}Total: fixed {total_fixed}, already ok {total_skipped}, no source {total_missing}")


if __name__ == "__main__":
    main()
