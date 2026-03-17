**English** | [Русский](README.ru.md)

# ffresque

Block-level file rescue from damaged media.

When a storage device has scattered bad sectors (a degraded RAID mirror, a failing disk, a corrupted ZFS pool), tools like `rsync` and `cp` stall or abort on I/O errors. **ffresque** copies files block-by-block, fills unreadable blocks with zeros, and tracks every block's status in a SQLite database. On re-run — possibly from a different copy of the same data (e.g. the other disk of a broken mirror) — only previously failed blocks are retried.

Think of it as [ddrescue](https://www.gnu.org/software/ddrescue/), but operating at the file level instead of raw disk level.

## Requirements

- Python 3.8+
- No external dependencies (stdlib only)

## Quick start

```bash
# 1. Copy all files from a damaged source
python3 ffresque.py copy \
  --src /mnt/damaged \
  --work-dir /mnt/recovery/incomplete \
  --dst /mnt/recovery/recovered

# 2. Or provide a list of specific files to recover
python3 ffresque.py copy \
  --src /mnt/damaged \
  --work-dir /mnt/recovery/incomplete \
  --dst /mnt/recovery/recovered \
  --bad-files bad-files.txt

# 3. Check progress
python3 ffresque.py status --db blocks.db

# 4. Re-run from a different source (retries only bad blocks)
python3 ffresque.py copy \
  --src /mnt/damaged-disk-b \
  --work-dir /mnt/recovery/incomplete \
  --dst /mnt/recovery/recovered \
  --db blocks.db
```

## How it works

```
src (damaged media) ──read──▶ work-dir (incomplete files) ──move──▶ dst (complete files)
                                   │
                                   ▼
                              blocks.db (block status: ok / bad)
```

1. Each file from `--bad-files` is read in `--block-size` chunks (default 128K).
2. Readable blocks are written to `--work-dir`, preserving the directory structure. Unreadable blocks are filled with zeros.
3. Block status (`ok` or `bad`) is stored in a SQLite database (`--db`).
4. When all blocks of a file become `ok`, the file is moved from `--work-dir` to `--dst`. File metadata (mtime, permissions, ownership) is preserved from the source.
5. On subsequent runs with the same `--db`, only `bad` blocks are retried. This allows recovery from a different copy of the data (e.g. the other half of a mirror).
6. Paths of newly completed files are appended to `--done-file`.
7. Files already present in `--dst` are skipped (see `--skip-existing`).

## Commands

### `copy`

```
python3 ffresque.py copy \
  --src SRC \
  --work-dir WORK_DIR \
  --dst DST \
  [--bad-files bad-files.txt] \
  [--block-size 131072] \
  [--db blocks.db] \
  [--done-file done-files.txt] \
  [--skip-existing | --no-skip-existing] \
  [--skip-bad-blocks | --no-skip-bad-blocks] \
  [--no-times] [--no-perms] [--no-owner]
```

| Argument | Required | Default | Description |
|---|---|---|---|
| `--src` | yes | | Source directory on damaged media |
| `--work-dir` | yes | | Working directory for incomplete files; bad blocks are filled with zeros |
| `--dst` | yes | | Final destination; files are moved here when fully recovered |
| `--bad-files` | no | scan `--src` | Text file listing files to recover (paths relative to `--src`, one per line). If omitted, all files in `--src` are processed |
| `--block-size` | no | 131072 | Read block size in bytes (128K, should match filesystem record size). Do not change on re-run, delete database and incomplete files first, and start from scratch |
| `--db` | no | `blocks.db` | SQLite database for block tracking |
| `--done-file` | no | `done-files.txt` | File to append fully recovered paths to |
| `--skip-existing` | no | enabled | Skip files already present in `--dst`; disable with `--no-skip-existing` |
| `--skip-bad-blocks` | no | disabled | Skip files where all blocks have been attempted, even if some are still bad; enable with `--skip-bad-blocks` |
| `--no-times` | no | off | Do not preserve mtime/atime from the source |
| `--no-perms` | no | off | Do not preserve file permissions (mode) from the source |
| `--no-owner` | no | off | Do not preserve uid/gid ownership from the source (chown requires root) |

#### Metadata preservation

By default, ffresque copies the source file's modification time (mtime), permissions (mode), and ownership (uid/gid) to both the work file and the final destination. Ownership requires root; if `chown` fails, the error is silently ignored.

Use `--no-times`, `--no-perms`, or `--no-owner` to disable individual attributes.

#### `--skip-existing`

Enabled by default. When a file already exists in `--dst`, it is assumed to be fully recovered and skipped entirely. Disable with `--no-skip-existing` to force reprocessing (e.g. if a file in `--dst` was corrupted after recovery).

#### `--skip-bad-blocks`

Disabled by default. When enabled, files where every block has already been read at least once (regardless of whether some are `bad`) are skipped. This is useful when re-running against the **same** source — bad blocks that are permanently unreadable won't be retried. When switching to a **different** source (e.g. another disk from a mirror), keep this flag off so that bad blocks are retried from the new media.

### `status`

```
python3 ffresque.py status [--db blocks.db]
```

Prints recovery statistics from the database: total/complete/damaged file counts, top 10 most damaged files, and overall recovery rate.

## Recovery from multiple sources

The main use case for re-runs: a broken mirror where each disk has different bad sectors.

```bash
# Source A — first pass
python3 ffresque.py copy \
  --src /mnt/disk-a \
  --work-dir /mnt/recovery/incomplete \
  --dst /mnt/recovery/recovered \
  --bad-files bad.txt \
  --db blocks.db

# Source B — only retries blocks that failed on disk A
python3 ffresque.py copy \
  --src /mnt/disk-b \
  --work-dir /mnt/recovery/incomplete \
  --dst /mnt/recovery/recovered \
  --bad-files bad.txt \
  --db blocks.db
```

After both runs, files where the two disks had bad blocks at different offsets will be fully recovered.

## Use cases

- **Dying HDD with bad sectors** — copy photos, documents, and videos off a failing hard drive before it dies completely. Unlike `cp` or `rsync`, ffresque won't hang on unreadable sectors.
- **Degraded RAID1 / ZFS mirror** — one disk is dead, the surviving disk has scattered bad sectors. Run ffresque on disk A, then swap to disk B and re-run — bad sectors at different offsets get filled in from the other disk.
- **ZFS / BTRFS pool with data errors** — `zpool status` or `btrfs scrub` reports checksum errors. Mount the pool readonly and rescue files block by block.
- **NAS data recovery** — old Synology / QNAP / TrueNAS with degraded array. Pull the disks, mount them on a Linux box, and rescue what you can.
- **Disk image recovery** — working with `dd` images of damaged disks via loopback mount. ffresque handles I/O errors from the image transparently.
- **SD card / CF card / USB flash drive** — corrupted camera card or flash drive with unreadable sectors. Recover as many photos as possible.
- **rsync fails with I/O error** — `rsync: read errors mapping: Input/output error`. ffresque skips bad blocks instead of aborting the entire file.
- **Partial file recovery** — even if some blocks are permanently lost, the rest of the file is saved. For media files (JPEG, MP4) this often means most of the content is still usable.
- **Combining multiple backup copies** — you have several old backups of the same directory tree, each with different corruptions. Run ffresque against each source to assemble the most complete version.
- **Pre-migration rescue** — before decommissioning an old server or array, copy everything off with block-level tracking so you know exactly what was and wasn't recovered.

## Utilities

### `fix_mtime.py`

Retroactively restores metadata (mtime, permissions, ownership) on files that were recovered before metadata preservation was added to ffresque. Walks `--dst` and `--work-dir`, finds matching files in `--src`, and applies the source metadata.

```bash
# Dry run — see what would change
python3 fix_mtime.py --src /mnt/damaged --dst /mnt/recovered --work-dir /tmp/work --dry-run

# Apply
python3 fix_mtime.py --src /mnt/damaged --dst /mnt/recovered --work-dir /tmp/work

# Only fix times, skip permissions and ownership
python3 fix_mtime.py --src /mnt/damaged --dst /mnt/recovered --no-perms --no-owner
```

| Argument | Required | Default | Description |
|---|---|---|---|
| `--src` | yes | | Source directory (original files) |
| `--dst` | yes | | Destination directory (fully recovered files) |
| `--work-dir` | no | | Work directory (incomplete files) |
| `--dry-run` | no | off | Show what would be changed without applying |
| `--no-times` | no | off | Do not restore mtime/atime |
| `--no-perms` | no | off | Do not restore file permissions (mode) |
| `--no-owner` | no | off | Do not restore uid/gid ownership |

## Testing

```bash
pytest test_ffresque.py -v
```

## Database schema

```sql
-- Per-block status
CREATE TABLE blocks (
    file TEXT NOT NULL,
    block_num INTEGER NOT NULL,
    status TEXT NOT NULL,        -- 'ok' | 'bad'
    updated_at TEXT,
    PRIMARY KEY (file, block_num)
);

-- Per-file summary
CREATE TABLE files (
    file TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    total_blocks INTEGER NOT NULL,
    ok_blocks INTEGER DEFAULT 0,
    bad_blocks INTEGER DEFAULT 0,
    complete BOOLEAN DEFAULT 0
);
```
