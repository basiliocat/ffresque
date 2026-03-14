# ffresque

Block-level file rescue from damaged media.

When a storage device has scattered bad sectors (a degraded RAID mirror, a failing disk, a corrupted ZFS pool), tools like `rsync` and `cp` stall or abort on I/O errors. **ffresque** copies files block-by-block, fills unreadable blocks with zeros, and tracks every block's status in a SQLite database. On re-run — possibly from a different copy of the same data (e.g. the other disk of a broken mirror) — only previously failed blocks are retried.

Think of it as [ddrescue](https://www.gnu.org/software/ddrescue/), but operating at the file level instead of raw disk level.

## Requirements

- Python 3.8+
- No external dependencies (stdlib only)

## Quick start

```bash
# 1. Prepare a list of damaged files (relative paths, one per line)
find /mnt/damaged -type f > bad-files.txt
# ... or however you generate yours

# 2. First run
python3 ffresque.py copy \
  --src /mnt/damaged \
  --work-dir /mnt/recovery/work \
  --dst /mnt/recovery/done \
  --bad-files bad-files.txt

# 3. Check progress
python3 ffresque.py status --db blocks.db

# 4. Re-run from a different source (retries only bad blocks)
python3 ffresque.py copy \
  --src /mnt/damaged-disk-b \
  --work-dir /mnt/recovery/work \
  --dst /mnt/recovery/done \
  --bad-files bad-files.txt \
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

## Commands

### `copy`

```
python3 ffresque.py copy \
  --src SRC \
  --work-dir WORK_DIR \
  --dst DST \
  --bad-files BAD_FILES \
  [--block-size 131072] \
  [--db blocks.db] \
  [--done-file done-files.txt]
```

| Argument | Required | Description |
|---|---|---|
| `--src` | yes | Source directory on damaged media |
| `--work-dir` | yes | Working directory for incomplete files; bad blocks are filled with zeros |
| `--dst` | yes | Final destination; files are moved here when fully recovered |
| `--bad-files` | yes | Text file listing damaged files (paths relative to `--src`, one per line) |
| `--block-size` | no | Read block size in bytes (default: 131072 = 128K) |
| `--db` | no | SQLite database for block tracking (default: `blocks.db`) |
| `--done-file` | no | File to append fully recovered paths to (default: `done-files.txt`) |

### `status`

```
python3 ffresque.py status [--db blocks.db]
```

Prints recovery statistics from the database: total/complete/damaged file counts, top 10 worst files, and overall recovery rate.

## Recovery from multiple sources

The main use case for re-runs: a broken mirror where each disk has different bad sectors.

```bash
# Source A
python3 ffresque.py copy --src /mnt/disk-a --work-dir /work --dst /recovered --bad-files bad.txt --db blocks.db

# Source B — only retries blocks that failed on disk A
python3 ffresque.py copy --src /mnt/disk-b --work-dir /work --dst /recovered --bad-files bad.txt --db blocks.db
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
