"""Tests for ffresque.py"""

import os
import sqlite3
import tempfile
import textwrap

import pytest

import ffresque as zbc


@pytest.fixture
def tmp(tmp_path):
    """Create src / work / dst directories and return a namespace."""

    class Dirs:
        src = tmp_path / "src"
        work = tmp_path / "work"
        dst = tmp_path / "dst"
        db = str(tmp_path / "blocks.db")
        done = str(tmp_path / "done.txt")
        bad_files = str(tmp_path / "bad-files.txt")

    Dirs.src.mkdir()
    Dirs.work.mkdir()
    Dirs.dst.mkdir()
    return Dirs


def write_src(dirs, rel_path, data):
    """Helper: write a file under dirs.src."""
    p = dirs.src / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def write_bad_files(dirs, paths):
    """Helper: write bad-files.txt."""
    with open(dirs.bad_files, "w") as f:
        for p in paths:
            f.write(p + "\n")


# ── Basic copy ──────────────────────────────────────────────────────


class TestBasicCopy:
    def test_single_file_exact_blocks(self, tmp):
        """File whose size is an exact multiple of block_size."""
        data = b"A" * 1024 + b"B" * 1024
        write_src(tmp, "photo.jpg", data)
        write_bad_files(tmp, ["photo.jpg"])

        conn = zbc.open_db(tmp.db)
        ok, bad, complete, skipped = zbc.process_file(
            "photo.jpg", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn
        )
        conn.commit()
        conn.close()

        assert ok == 2
        assert bad == 0
        assert complete is True
        assert skipped is False
        # File should be in dst (moved from work)
        assert (tmp.dst / "photo.jpg").read_bytes() == data
        assert not (tmp.work / "photo.jpg").exists()

    def test_single_file_short_last_block(self, tmp):
        """File whose last block is shorter than block_size."""
        data = b"X" * 1500
        write_src(tmp, "file.bin", data)
        write_bad_files(tmp, ["file.bin"])

        conn = zbc.open_db(tmp.db)
        ok, bad, complete, skipped = zbc.process_file(
            "file.bin", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn
        )
        conn.commit()
        conn.close()

        assert ok == 2
        assert complete is True
        assert (tmp.dst / "file.bin").read_bytes() == data

    def test_empty_file(self, tmp):
        """Empty file goes directly to dst."""
        write_src(tmp, "empty.txt", b"")
        write_bad_files(tmp, ["empty.txt"])

        conn = zbc.open_db(tmp.db)
        ok, bad, complete, skipped = zbc.process_file(
            "empty.txt", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn
        )
        conn.commit()
        conn.close()

        assert complete is True
        assert skipped is False
        assert (tmp.dst / "empty.txt").exists()
        assert (tmp.dst / "empty.txt").stat().st_size == 0

    def test_nested_directory(self, tmp):
        """Subdirectories are created automatically."""
        data = b"D" * 512
        write_src(tmp, "a/b/c/deep.bin", data)
        write_bad_files(tmp, ["a/b/c/deep.bin"])

        conn = zbc.open_db(tmp.db)
        zbc.process_file(
            "a/b/c/deep.bin", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn
        )
        conn.commit()
        conn.close()

        assert (tmp.dst / "a" / "b" / "c" / "deep.bin").read_bytes() == data


# ── Re-run behaviour ───────────────────────────────────────────────


class TestRerun:
    def test_rerun_skips_ok_blocks(self, tmp):
        """Second run reads zero blocks for a fully recovered file."""
        data = b"R" * 2048
        write_src(tmp, "rerun.bin", data)

        conn = zbc.open_db(tmp.db)
        ok1, _, _, _ = zbc.process_file(
            "rerun.bin", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn
        )
        conn.commit()

        ok2, bad2, complete2, skipped2 = zbc.process_file(
            "rerun.bin", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn
        )
        conn.commit()
        conn.close()

        assert ok1 == 2
        # Second run: file already in dst, returns immediately
        assert ok2 == 0
        assert complete2 is True
        assert skipped2 is True

    def test_rerun_retries_bad_blocks(self, tmp):
        """Manually mark a block as bad, then re-run — it should be retried."""
        data = b"A" * 1024 + b"B" * 1024
        write_src(tmp, "retry.bin", data)

        conn = zbc.open_db(tmp.db)

        # Simulate first run: block 0 ok, block 1 bad
        zbc.upsert_block(conn, "retry.bin", 0, "ok")
        zbc.upsert_block(conn, "retry.bin", 1, "bad")
        zbc.upsert_file(conn, "retry.bin", 2048, 2, 1, 1)
        conn.commit()

        # Create partial file in work-dir (block 0 ok, block 1 zeros)
        work_path = tmp.work / "retry.bin"
        work_path.parent.mkdir(parents=True, exist_ok=True)
        with open(work_path, "wb") as f:
            f.write(b"A" * 1024 + b"\x00" * 1024)

        # Re-run: should only retry block 1
        ok, bad, complete, skipped = zbc.process_file(
            "retry.bin", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn
        )
        conn.commit()
        conn.close()

        assert ok == 1  # only block 1 retried
        assert bad == 0
        assert complete is True
        assert skipped is False
        assert (tmp.dst / "retry.bin").read_bytes() == data


# ── File not found ──────────────────────────────────────────────────


class TestMissingFile:
    def test_missing_source_skipped(self, tmp, capsys):
        """File that doesn't exist in src is skipped."""
        write_bad_files(tmp, ["no-such-file.bin"])

        conn = zbc.open_db(tmp.db)
        ok, bad, complete, skipped = zbc.process_file(
            "no-such-file.bin", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn
        )
        conn.close()

        assert ok == 0
        assert complete is False
        assert skipped is True
        captured = capsys.readouterr()
        assert "SKIP" in captured.err


# ── Skip flags ──────────────────────────────────────────────────────


class TestSkipExisting:
    def test_file_already_in_dst_skipped_by_default(self, tmp):
        """With skip_existing=True (default), file in dst is skipped."""
        data = b"Z" * 500
        write_src(tmp, "done.bin", data)
        (tmp.dst / "done.bin").write_bytes(data)

        conn = zbc.open_db(tmp.db)
        ok, bad, complete, skipped = zbc.process_file(
            "done.bin", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn,
            skip_existing=True,
        )
        conn.close()

        assert ok == 0
        assert complete is True
        assert skipped is True

    def test_file_in_dst_reprocessed_when_flag_off(self, tmp):
        """With skip_existing=False, file in dst is reprocessed."""
        data = b"Z" * 500
        write_src(tmp, "redo.bin", data)
        (tmp.dst / "redo.bin").write_bytes(b"\x00" * 500)  # stale copy in dst

        conn = zbc.open_db(tmp.db)
        ok, bad, complete, skipped = zbc.process_file(
            "redo.bin", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn,
            skip_existing=False,
        )
        conn.commit()
        conn.close()

        assert ok == 1
        assert skipped is False


class TestSkipAttempted:
    def test_all_blocks_attempted_with_bad_skipped(self, tmp):
        """skip_attempted=True skips files where all blocks were tried."""
        write_src(tmp, "tried.bin", b"A" * 2048)

        conn = zbc.open_db(tmp.db)
        # Simulate: 2 blocks, block 0 ok, block 1 bad — all attempted
        zbc.upsert_block(conn, "tried.bin", 0, "ok")
        zbc.upsert_block(conn, "tried.bin", 1, "bad")
        zbc.upsert_file(conn, "tried.bin", 2048, 2, 1, 1)
        conn.commit()

        ok, bad, complete, skipped = zbc.process_file(
            "tried.bin", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn,
            skip_attempted=True,
        )
        conn.close()

        assert ok == 0
        assert skipped is True
        assert complete is False

    def test_all_blocks_attempted_retried_by_default(self, tmp):
        """skip_attempted=False (default) retries bad blocks."""
        data = b"A" * 1024 + b"B" * 1024
        write_src(tmp, "retry2.bin", data)

        conn = zbc.open_db(tmp.db)
        zbc.upsert_block(conn, "retry2.bin", 0, "ok")
        zbc.upsert_block(conn, "retry2.bin", 1, "bad")
        zbc.upsert_file(conn, "retry2.bin", 2048, 2, 1, 1)
        conn.commit()

        # Create partial file in work-dir
        work_path = tmp.work / "retry2.bin"
        with open(work_path, "wb") as f:
            f.write(b"A" * 1024 + b"\x00" * 1024)

        ok, bad, complete, skipped = zbc.process_file(
            "retry2.bin", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn,
            skip_attempted=False,
        )
        conn.commit()
        conn.close()

        assert ok == 1  # block 1 retried and recovered
        assert skipped is False
        assert complete is True

    def test_partially_attempted_not_skipped(self, tmp):
        """skip_attempted=True does NOT skip if some blocks are unattempted."""
        data = b"A" * 1024 + b"B" * 1024
        write_src(tmp, "partial.bin", data)

        conn = zbc.open_db(tmp.db)
        # Only block 0 attempted, block 1 not in DB yet
        zbc.upsert_block(conn, "partial.bin", 0, "ok")
        zbc.upsert_file(conn, "partial.bin", 2048, 2, 1, 0)
        conn.commit()

        ok, bad, complete, skipped = zbc.process_file(
            "partial.bin", str(tmp.src), str(tmp.work), str(tmp.dst), 1024, conn,
            skip_attempted=True,
        )
        conn.commit()
        conn.close()

        assert ok == 1  # block 1 was read
        assert skipped is False
        assert complete is True


# ── Database ────────────────────────────────────────────────────────


class TestDatabase:
    def test_schema_created(self, tmp):
        conn = zbc.open_db(tmp.db)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()
        assert "blocks" in tables
        assert "files" in tables

    def test_block_upsert_updates_status(self, tmp):
        conn = zbc.open_db(tmp.db)
        zbc.upsert_block(conn, "f.txt", 0, "bad")
        zbc.upsert_block(conn, "f.txt", 0, "ok")
        conn.commit()

        row = conn.execute(
            "SELECT status FROM blocks WHERE file='f.txt' AND block_num=0"
        ).fetchone()
        conn.close()
        assert row[0] == "ok"

    def test_file_complete_flag(self, tmp):
        conn = zbc.open_db(tmp.db)
        # All ok
        assert zbc.upsert_file(conn, "a.txt", 2048, 2, 2, 0) is True
        # Has bad blocks
        assert zbc.upsert_file(conn, "b.txt", 2048, 2, 1, 1) is False
        conn.close()


# ── move_to_dst ─────────────────────────────────────────────────────


class TestMoveToDst:
    def test_move_creates_parent_dirs(self, tmp):
        src_file = tmp.work / "x" / "y" / "z.bin"
        src_file.parent.mkdir(parents=True)
        src_file.write_bytes(b"hello")

        zbc.move_to_dst("x/y/z.bin", str(tmp.work), str(tmp.dst))

        assert (tmp.dst / "x" / "y" / "z.bin").read_bytes() == b"hello"
        assert not src_file.exists()

    def test_move_noop_when_source_missing(self, tmp):
        # Should not raise
        zbc.move_to_dst("nonexistent.bin", str(tmp.work), str(tmp.dst))


# ── human_size ──────────────────────────────────────────────────────


class TestHumanSize:
    @pytest.mark.parametrize(
        "nbytes, expected",
        [
            (0, "0.0 B"),
            (1023, "1023.0 B"),
            (1024, "1.0 KiB"),
            (1024 * 1024, "1.0 MiB"),
            (1024 ** 3, "1.0 GiB"),
            (1.5 * 1024 ** 3, "1.5 GiB"),
        ],
    )
    def test_human_size(self, nbytes, expected):
        assert zbc.human_size(nbytes) == expected


# ── cmd_copy integration ────────────────────────────────────────────


class TestCmdCopy:
    def test_full_session(self, tmp):
        """End-to-end: cmd_copy processes multiple files."""
        write_src(tmp, "a.bin", b"A" * 3000)
        write_src(tmp, "sub/b.bin", b"B" * 1024)
        write_bad_files(tmp, ["a.bin", "sub/b.bin"])

        class Args:
            src = str(tmp.src)
            work_dir = str(tmp.work)
            dst = str(tmp.dst)
            bad_files = tmp.bad_files
            block_size = 1024
            db = tmp.db
            done_file = tmp.done
            skip_existing = True
            skip_attempted = False

        zbc.cmd_copy(Args())

        assert (tmp.dst / "a.bin").read_bytes() == b"A" * 3000
        assert (tmp.dst / "sub" / "b.bin").read_bytes() == b"B" * 1024
        # done-file should list both
        done = open(tmp.done).read().splitlines()
        assert "a.bin" in done
        assert "sub/b.bin" in done

    def test_empty_bad_files(self, tmp, capsys):
        """Empty bad-files list prints message and exits."""
        write_bad_files(tmp, [])

        class Args:
            src = str(tmp.src)
            work_dir = str(tmp.work)
            dst = str(tmp.dst)
            bad_files = tmp.bad_files
            block_size = 1024
            db = tmp.db
            done_file = tmp.done
            skip_existing = True
            skip_attempted = False

        zbc.cmd_copy(Args())
        assert "No files" in capsys.readouterr().out

    def test_no_bad_files_scans_src(self, tmp):
        """Without --bad-files, all files in src are processed."""
        write_src(tmp, "x.bin", b"X" * 512)
        write_src(tmp, "sub/y.bin", b"Y" * 256)

        class Args:
            src = str(tmp.src)
            work_dir = str(tmp.work)
            dst = str(tmp.dst)
            bad_files = None
            block_size = 1024
            db = tmp.db
            done_file = tmp.done
            skip_existing = True
            skip_attempted = False

        zbc.cmd_copy(Args())

        assert (tmp.dst / "x.bin").read_bytes() == b"X" * 512
        assert (tmp.dst / "sub" / "y.bin").read_bytes() == b"Y" * 256
