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
4. When all blocks of a file become `ok`, the file is moved from `--work-dir` to `--dst`.
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
  [--skip-attempted | --no-skip-attempted]
```

| Argument | Required | Default | Description |
|---|---|---|---|
| `--src` | yes | | Source directory on damaged media |
| `--work-dir` | yes | | Working directory for incomplete files; bad blocks are filled with zeros |
| `--dst` | yes | | Final destination; files are moved here when fully recovered |
| `--bad-files` | no | scan `--src` | Text file listing files to recover (paths relative to `--src`, one per line). If omitted, all files in `--src` are processed |
| `--block-size` | no | 131072 | Read block size in bytes (128K, should match filesystem record size). Do not change, if SQLite database already exists, delete db first, and start from scratch |
| `--db` | no | `blocks.db` | SQLite database for block tracking |
| `--done-file` | no | `done-files.txt` | File to append fully recovered paths to |
| `--skip-existing` | no | enabled | Skip files already present in `--dst`; disable with `--no-skip-existing` |
| `--skip-attempted` | no | disabled | Skip files where all blocks have been attempted, even if some are still bad; enable with `--skip-attempted` |

#### `--skip-existing`

Enabled by default. When a file already exists in `--dst`, it is assumed to be fully recovered and skipped entirely. Disable with `--no-skip-existing` to force reprocessing (e.g. if a file in `--dst` was corrupted after recovery).

#### `--skip-attempted`

Disabled by default. When enabled, files where every block has already been read at least once (regardless of whether some are `bad`) are skipped. This is useful when re-running against the **same** source — bad blocks that are permanently unreadable won't be retried. When switching to a **different** source (e.g. another disk from a mirror), keep this flag off so that bad blocks are retried from the new media.

### `status`

```
python3 ffresque.py status [--db blocks.db]
```

Prints recovery statistics from the database: total/complete/damaged file counts, top 10 worst files, and overall recovery rate.

## Output

### Starting banner

```
=== Starting ===
Files in list: 4251
Already complete: 3800
To process: 451
Bad blocks to retry: 678 (84.8 MiB)
```

### Progress (overwrites in-place on TTY)

```
  [200/4251] 5% | 3m45s, ETA 10m24s | +1234 ok, +56 bad (4.3%), +45 complete
```

### Session summary

```
=== Session Summary (12m05s) ===
Files in list: 4251
Skipped: 3800
Blocks read OK (this session): 1234 (154.2 MiB)
Blocks still bad: 56 (7.0 MiB)
Files fully recovered: 4195/4251
Files with remaining bad blocks: 56
New files completed this session: 395
  (moved to /mnt/recovery/recovered)
```

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
