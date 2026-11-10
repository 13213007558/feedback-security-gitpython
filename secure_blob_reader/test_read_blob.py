import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from secure_blob_reader.read_blob import read_blob, read_path_at_commit

REPO = Path(os.environ["REPO_PATH"])
MARKERS = Path(os.environ["SEC3_MARKER_DIR"])
BLOB = "00581e5b21c9891dfac05dadcb564fa7b8e5ef2c"
COMMIT = "96d026482fb34668cdb5bdaa5c33275fef0710c3"
TRUSTED_GIT = "/usr/bin/git"


def test_reads_raw_blob_without_running_smudge_filter():
    before = len(list(MARKERS.glob("smudge-*")))
    result = read_blob(str(REPO), BLOB)
    after = len(list(MARKERS.glob("smudge-*")))
    assert result.data == b"secret-ish content v1\n"
    assert result.size == len(result.data)
    assert after == before  # filter driver never executed


def test_path_lookup_at_fixed_commit_also_skips_filters():
    result = read_path_at_commit(str(REPO), COMMIT, "payload.txt")
    assert result.hexsha == BLOB
    assert result.data == b"secret-ish content v1\n"


def test_rejects_non_full_sha():
    with pytest.raises(ValueError):
        read_blob(str(REPO), BLOB[:12])


def test_rejects_wrong_object_type():
    with pytest.raises(ValueError):  # COMMIT is a commit, not a blob
        read_blob(str(REPO), COMMIT)


def _run_in_subprocess(env_extra, code):
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True
    )


def test_pinned_binary_ignores_hostile_path(tmp_path):
    """A fake git shadowing PATH must never run when the binary is pinned."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = MARKERS / "hostile-git.log"
    if log.exists():
        log.unlink()
    (fakebin / "git").write_text(
        f"#!/bin/sh\necho called >> {log}\nexit 42\n"
    )
    (fakebin / "git").chmod(0o755)

    code = (
        "from secure_blob_reader.read_blob import read_blob;"
        "r = read_blob(%r, %r);"
        "print(r.data.decode().strip())" % (str(REPO), BLOB)
    )
    proc = _run_in_subprocess(
        {
            "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
            "GIT_PYTHON_GIT_EXECUTABLE": TRUSTED_GIT,
        },
        code,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "secret-ish content v1"
    assert not log.exists()


def test_import_time_probe_still_runs_without_pinned_binary(tmp_path):
    """Documents the residual boundary: importing GitPython probes `git version`.

    GIT_PYTHON_REFRESH=quiet only changes the reaction to a failed probe; the
    probe itself is unconditional (git/cmd.py refresh -> cls().version()).
    """
    fakebin = tmp_path / "fakebin2"
    fakebin.mkdir()
    (fakebin / "git").write_text("#!/bin/sh\nexit 42\n")
    (fakebin / "git").chmod(0o755)
    proc = _run_in_subprocess(
        {
            "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
            "GIT_PYTHON_REFRESH": "quiet",
        },
        "import git",
    )
    # With a hostile PATH and no pinned binary, the import-time probe fails.
    assert proc.returncode != 0
    assert "git version" in proc.stderr
