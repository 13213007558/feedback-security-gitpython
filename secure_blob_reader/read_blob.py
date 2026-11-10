"""Read a single blob at a fixed, full commit SHA without touching the work tree.

Design choices, with the audit findings that motivate them:

1. The object database is the pure-Python ``gitdb.db.GitDB`` backend
   (``odbt=GitDB``). Object/header reads then go through zlib + pure-Python
   pack/loose parsers and never spawn the ``git`` binary, so smudge/clean
   filters defined in repository or user configuration cannot run.
   The default backend ``git.db.GitCmdObjectDB`` instead shells out to a
   persistent ``git cat-file --batch`` process for every object read.

2. The blob is addressed by a *full 40-character SHA*. No ref, tag, partial
   abbreviation, reflog or symbolic name is resolved, which avoids the
   revision-parsing code paths that invoke git.

3. Nothing here performs a checkout / reset / read-tree into a work tree, so
   ``.gitattributes`` filters and hooks are never evaluated.

Trust boundary that remains:
   * Importing GitPython runs one unconditional ``git version`` probe, and any
     code path outside this module may invoke git. Pin the binary with the
     GIT_PYTHON_GIT_EXECUTABLE env var (or Git.refresh(abs_path)) to a trusted
     absolute path; do not rely on PATH. GIT_PYTHON_REFRESH=quiet only silences
     a failed probe, it does not skip it.
   * The process environment (GIT_CONFIG_*, HOME, XDG_CONFIG_HOME, PATH, ...) is
     inherited by any git subprocess GitPython does spawn; sanitize it.
   * ``objects/info/alternates`` can redirect object reads to other directories
     on disk; validate the repository layout before opening untrusted input.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from git import Repo
from git.db import GitDB

_SHA1_HEX_LEN = 40


@dataclass(frozen=True)
class BlobResult:
    hexsha: str
    type: bytes
    size: int
    data: bytes


def _is_full_sha1(hexsha: str) -> bool:
    if len(hexsha) != _SHA1_HEX_LEN:
        return False
    try:
        int(hexsha, 16)
    except ValueError:
        return False
    return True


def read_blob(repo_path: str, blob_sha: str) -> BlobResult:
    """Return the raw, unfiltered contents of the blob identified by ``blob_sha``.

    Raises ValueError for anything that is not a full 40-hex SHA and
    gitdb.exc.BadObject if the object is absent from the object database.
    """
    if not _is_full_sha1(blob_sha):
        raise ValueError(f"expected a full {_SHA1_HEX_LEN}-char SHA, got: {blob_sha!r}")

    repo = Repo(repo_path, odbt=GitDB)
    bin_sha = bytes.fromhex(blob_sha)

    info = repo.odb.info(bin_sha)
    if info.type != b"blob":
        raise ValueError(f"object {blob_sha} is {info.type!r}, not b'blob'")

    data = repo.odb.stream(bin_sha).read()

    # Defense in depth: confirm the bytes hash to the requested object id.
    actual = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()
    if actual != blob_sha:
        raise RuntimeError(f"content hash mismatch: requested {blob_sha}, got {actual}")

    return BlobResult(hexsha=blob_sha, type=info.type, size=info.size, data=data)


def read_path_at_commit(repo_path: str, commit_sha: str, path: str) -> BlobResult:
    """Read one path from a fixed, full commit SHA without checking anything out.

    The tree walk itself is pure-Python under the GitDB backend; only the commit
    and tree objects are read. The path must match an entry in that exact tree.
    """
    if not _is_full_sha1(commit_sha):
        raise ValueError(f"expected a full {_SHA1_HEX_LEN}-char commit SHA, got: {commit_sha!r}")

    repo = Repo(repo_path, odbt=GitDB)
    commit = repo.commit(commit_sha)
    entry = commit.tree / path
    return read_blob(repo_path, entry.hexsha)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("usage: read_blob.py <repo-path> <full-blob-sha>", file=sys.stderr)
        raise SystemExit(2)

    result = read_blob(sys.argv[1], sys.argv[2])
    sys.stdout.buffer.write(result.data)
    print(
        f"\n[read_blob] {result.hexsha} type={result.type.decode()} "
        f"size={result.size} GIT={os.environ.get('GIT_PYTHON_GIT_EXECUTABLE', '<PATH lookup>')}",
        file=sys.stderr,
    )
