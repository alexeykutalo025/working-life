"""Hashing, and telling identical files apart from merely similar ones."""

from __future__ import annotations

import hashlib
from pathlib import Path

from recall.scan.fingerprint import (
    DuplicateGroup,
    find_duplicate_groups,
    hash_bytes,
    hash_file,
    mark_duplicates,
    sha256_hex,
)


def test_hash_matches_blake2b_256(tmp_path: Path):
    p = tmp_path / "a.bin"
    data = b"the quick brown fox" * 1000
    p.write_bytes(data)
    expected = hashlib.blake2b(data, digest_size=32).hexdigest()
    assert hash_file(p).content_hash == expected


def test_hash_is_streamed_not_loaded(tmp_path: Path):
    """A file larger than one chunk still hashes correctly."""
    p = tmp_path / "big.bin"
    data = bytes(range(256)) * 20000  # ~5 MB, several chunks
    p.write_bytes(data)
    result = hash_file(p)
    assert result.content_hash == hashlib.blake2b(data, digest_size=32).hexdigest()
    assert result.bytes_read == len(data)


def test_identical_content_hashes_the_same(tmp_path: Path):
    a, b = tmp_path / "a.pst", tmp_path / "copy of a.pst"
    a.write_bytes(b"same bytes")
    b.write_bytes(b"same bytes")
    assert hash_file(a).content_hash == hash_file(b).content_hash


def test_one_different_byte_changes_the_hash(tmp_path: Path):
    a, b = tmp_path / "a.pst", tmp_path / "b.pst"
    a.write_bytes(b"aaaa")
    b.write_bytes(b"aaab")
    assert hash_file(a).content_hash != hash_file(b).content_hash


def test_empty_file_hashes(tmp_path: Path):
    p = tmp_path / "empty.pst"
    p.write_bytes(b"")
    result = hash_file(p)
    assert result.ok
    assert result.bytes_read == 0


def test_placeholder_is_never_hashed(tmp_path: Path):
    """Hashing means reading, and reading a placeholder means downloading it."""
    p = tmp_path / "cloud.pst"
    p.write_bytes(b"would be downloaded")
    result = hash_file(p, is_placeholder=True)
    assert result.content_hash is None
    assert result.bytes_read == 0
    assert "download" in result.skipped_reason


def test_size_cap_skips_without_reading(tmp_path: Path):
    p = tmp_path / "huge.pst"
    p.write_bytes(b"x" * 5000)
    result = hash_file(p, size_cap_bytes=1000)
    assert result.content_hash is None
    assert result.bytes_read == 0
    assert "limit" in result.skipped_reason


def test_size_cap_of_zero_means_no_cap(tmp_path: Path):
    p = tmp_path / "file.pst"
    p.write_bytes(b"x" * 5000)
    assert hash_file(p, size_cap_bytes=0).ok


def test_missing_file_reports_the_error(tmp_path: Path):
    result = hash_file(tmp_path / "gone.pst")
    assert result.content_hash is None
    assert result.error
    assert result.ok is False


def test_cancel_stops_cleanly(tmp_path: Path):
    p = tmp_path / "big.bin"
    p.write_bytes(b"x" * (3 * 1024 * 1024))
    result = hash_file(p, cancel=lambda: True)
    assert result.content_hash is None
    assert result.skipped_reason == "canceled"


def test_progress_is_reported(tmp_path: Path):
    p = tmp_path / "big.bin"
    p.write_bytes(b"x" * (3 * 1024 * 1024))
    seen: list[int] = []
    hash_file(p, progress=seen.append)
    assert seen and seen[-1] == 3 * 1024 * 1024


def test_hash_bytes_matches_hash_file(tmp_path: Path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"content")
    assert hash_bytes(b"content") == hash_file(p).content_hash


def test_sha256_hex_is_stable():
    assert sha256_hex("abc") == hashlib.sha256(b"abc").hexdigest()


# --- duplicate grouping ---------------------------------------------------


def _row(i, path, h, size=100):
    return {"id": i, "path": path, "content_hash": h, "size_bytes": size}


def test_duplicates_are_grouped_by_hash():
    groups = find_duplicate_groups([
        _row(1, r"C:\mail.pst", "aaa"),
        _row(2, r"C:\backup\copy of mail.pst", "aaa"),
        _row(3, r"C:\other.pst", "bbb"),
    ])
    assert len(groups) == 1
    assert groups[0].keeper_id == 1
    assert groups[0].duplicate_ids == [2]


def test_shortest_path_is_kept():
    """The original is usually nearer the top of the drive than its copies."""
    groups = find_duplicate_groups([
        _row(1, r"C:\Users\Tim\Documents\Old\Backup\Backup of mail.pst", "aaa"),
        _row(2, r"C:\mail.pst", "aaa"),
    ])
    assert groups[0].keeper_id == 2


def test_unhashed_files_are_not_grouped():
    """No hash means "not compared", which is not the same as "unique"."""
    assert find_duplicate_groups([
        _row(1, r"C:\a.pst", None),
        _row(2, r"C:\b.pst", None),
    ]) == []


def test_single_copy_is_not_a_duplicate():
    assert find_duplicate_groups([_row(1, r"C:\a.pst", "aaa")]) == []


def test_three_copies_give_two_duplicates():
    groups = find_duplicate_groups([
        _row(1, r"C:\a.pst", "aaa"),
        _row(2, r"C:\bb.pst", "aaa"),
        _row(3, r"C:\ccc.pst", "aaa"),
    ])
    assert len(groups[0].duplicate_ids) == 2
    assert groups[0].wasted_bytes == 200


def test_mark_duplicates_writes_and_is_idempotent(conn):
    for i, path in enumerate([r"C:\a.pst", r"C:\bb.pst"], start=1):
        conn.execute(
            "INSERT INTO source_files(id, path, ext, content_hash, size_bytes) "
            "VALUES (?, ?, '.pst', 'aaa', 100)",
            (i, path),
        )
    rows = [dict(r) for r in conn.execute("SELECT id, path, content_hash, size_bytes FROM source_files")]
    groups = find_duplicate_groups(rows)

    mark_duplicates(conn, groups)
    first = conn.execute("SELECT id, duplicate_of FROM source_files ORDER BY id").fetchall()
    mark_duplicates(conn, groups)
    second = conn.execute("SELECT id, duplicate_of FROM source_files ORDER BY id").fetchall()

    assert [tuple(r) for r in first] == [tuple(r) for r in second]
    assert first[0]["duplicate_of"] is None    # the keeper
    assert first[1]["duplicate_of"] == 1


def test_keeper_is_never_marked_as_a_duplicate(conn):
    # Insert the target row first: duplicate_of is a real foreign key.
    conn.execute(
        "INSERT INTO source_files(id, path, ext, content_hash) "
        "VALUES (2, 'bb.pst', '.pst', 'aaa')"
    )
    conn.execute(
        "INSERT INTO source_files(id, path, ext, content_hash, duplicate_of) "
        "VALUES (1, 'a.pst', '.pst', 'aaa', 2)"
    )
    group = DuplicateGroup("aaa", keeper_id=1, keeper_path="a.pst", duplicate_ids=[2], size_bytes=0)
    mark_duplicates(conn, [group])
    assert conn.execute("SELECT duplicate_of FROM source_files WHERE id=1").fetchone()[0] is None
    assert conn.execute("SELECT duplicate_of FROM source_files WHERE id=2").fetchone()[0] == 1
