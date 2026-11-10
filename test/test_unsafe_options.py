# Copyright (C) 2026 Michael Trier (mtrier@gmail.com) and contributors
#
# This module is part of GitPython and is released under the
# 3-Clause BSD License: https://opensource.org/license/bsd-3-clause/

"""Regression tests for unsafe-option checks across clone, fetch, pull and push.

The checks must run against the exact argv representation handed to the git
process. A recording stand-in for ``git`` is used so these tests prove that a
rejected option never reaches a subprocess, without running any real git helper.
"""

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from git import Git, Repo
from git.exc import GitCommandError, UnsafeOptionError
STUB_SOURCE = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GIT_LOG"], "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
# Housekeeping GitPython issues when (re)opening an existing repository.
if sys.argv[1:2] in (["version"], ["cat-file"], ["rev-parse"],
                     ["symbolic-ref"], ["config"]):
    sys.exit(0)
sys.exit(2)
"""


class TestUnsafeOptions:
    def _setup(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.log = root / "argv.log"
        stub = root / "fake-git"
        stub.write_text(STUB_SOURCE)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        source = root / "source"
        # Build the local fixtures with the real git executable.
        self.repo = Repo.init(source)
        self.remote = self.repo.create_remote("origin", str(source))
        self.destination = root / "destination"
        self.repo.close()

        # Only swap in the recording stand-in once the fixtures exist.
        self._real_git = shutil.which(Git.GIT_PYTHON_GIT_EXECUTABLE) or Git.GIT_PYTHON_GIT_EXECUTABLE
        os.environ["FAKE_GIT_LOG"] = str(self.log)
        Git.refresh(str(stub))

    def _teardown(self):
        Git.refresh(self._real_git)
        os.environ.pop("FAKE_GIT_LOG", None)
        self._tmp.cleanup()

    def setup_method(self):
        self._setup()

    def teardown_method(self):
        self._teardown()

    def _recorded_argv(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def _assert_blocked_before_exec(self, label, call):
        if self.log.exists():
            self.log.unlink()
        try:
            call()
        except UnsafeOptionError:
            pass
        except Exception as ex:  # pragma: no cover - a failure here is a regression.
            raise AssertionError(f"{label}: expected UnsafeOptionError, got {ex!r}")
        assert not self._recorded_argv(), f"{label}: unsafe option reached a subprocess"

    def test_clone_blocks_hidden_combined_multi_option(self):
        self._assert_blocked_before_exec(
            "clone combined multi",
            lambda: Repo.clone_from(
                self.remote.url,
                self.destination,
                multi_options=["--depth 1 --upload-pack=EVIL"],
            ),
        )

    def test_clone_blocks_leading_whitespace_multi_option(self):
        self._assert_blocked_before_exec(
            "clone leading whitespace",
            lambda: Repo.clone_from(
                self.remote.url,
                self.destination,
                multi_options=["   --upload-pack=EVIL"],
            ),
        )

    def test_clone_blocks_all_unsafe_representations(self):
        cases = [
            ["--upload-pack=EVIL"],
            ["-u EVIL"],
            ["--config=protocol.ext.allow=always"],
            ["-c protocol.ext.allow=always"],
        ]
        for multi_options in cases:
            self._assert_blocked_before_exec(
                f"clone multi {multi_options}",
                lambda multi_options=multi_options: Repo.clone_from(
                    self.remote.url, self.destination, multi_options=multi_options
                ),
            )

        kwargs_cases = [
            {"upload_pack": "EVIL"},
            {"upload-pack": "EVIL"},
            {"u": "EVIL"},
            {"config": "protocol.ext.allow=always"},
            {"c": "protocol.ext.allow=always"},
        ]
        for kwargs in kwargs_cases:
            self._assert_blocked_before_exec(
                f"clone kwargs {kwargs}",
                lambda kwargs=kwargs: Repo.clone_from(
                    self.remote.url, self.destination, **kwargs
                ),
            )

    def test_fetch_pull_push_block_underscore_kwargs(self):
        self._assert_blocked_before_exec(
            "fetch upload_pack", lambda: self.remote.fetch(upload_pack="EVIL")
        )
        self._assert_blocked_before_exec(
            "pull upload_pack", lambda: self.remote.pull(upload_pack="EVIL")
        )
        self._assert_blocked_before_exec(
            "push receive_pack", lambda: self.remote.push(receive_pack="EVIL")
        )
        self._assert_blocked_before_exec(
            "push exec", lambda: self.remote.push(**{"exec": "EVIL"})
        )
        self._assert_blocked_before_exec(
            "push hyphen key",
            lambda: self.remote.push(**{"receive-pack": "EVIL"}),
        )

    def test_allow_unsafe_options_still_reaches_subprocess(self):
        if self.log.exists():
            self.log.unlink()
        try:
            Repo.clone_from(
                self.remote.url,
                self.destination,
                multi_options=["--depth 1 --upload-pack=EVIL"],
                allow_unsafe_options=True,
            )
        except GitCommandError:
            pass  # Expected: the stub rejects the actual invocation.
        assert self._recorded_argv(), "allow_unsafe_options must not block the subprocess"

        for call in (
            lambda: self.remote.fetch(upload_pack="EVIL", allow_unsafe_options=True),
            lambda: self.remote.pull(upload_pack="EVIL", allow_unsafe_options=True),
            lambda: self.remote.push(receive_pack="EVIL", allow_unsafe_options=True),
        ):
            if self.log.exists():
                self.log.unlink()
            try:
                call()
            except GitCommandError:
                pass
            assert self._recorded_argv(), "allow_unsafe_options must not block the subprocess"

    def test_safe_values_refspecs_and_paths_are_not_scanned(self):
        # A safe option whose VALUE contains the text of an unsafe option is allowed.
        if self.log.exists():
            self.log.unlink()
        try:
            Repo.clone_from(
                self.remote.url,
                self.destination,
                branch="--upload-pack=EVIL",
            )
        except GitCommandError:
            pass
        argv = self._recorded_argv()
        assert argv and "--branch=--upload-pack=EVIL" in argv[0]

        # Refspec content (a positional argument after "--") is not scanned.
        if self.log.exists():
            self.log.unlink()
        try:
            self.remote.fetch(refspec=["refs/heads/--upload-pack:refs/heads/x"])
        except GitCommandError:
            pass
        argv = self._recorded_argv()
        assert argv and "refs/heads/--upload-pack:refs/heads/x" in argv[0]

        # A destination path beginning with "--" (positional) is not scanned.
        if self.log.exists():
            self.log.unlink()
        try:
            Repo.clone_from(self.remote.url, self.destination.parent / "--dash-path")
        except GitCommandError:
            pass
        argv = self._recorded_argv()
        assert argv and str(self.destination.parent / "--dash-path") in argv[0]
