# Feedback task starting environment

Upstream: https://github.com/gitpython-developers/GitPython
Upstream commit: `6ba2c0a2f9ee7feffd7e079621c4845820180c9a`
Prepared on macOS with CPython 3.12.11. No external database, queue, or cloud account is required.
The dependency file pins the installed runtime and test packages for the selected baseline. It is not a claim that all optional upstream test suites are installed.

From the repository root:

```sh
export NO_INTERNET=1 TZ=UTC
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .feedback/requirements.txt -e .
.venv/bin/python -m pytest -q -o addopts= test/test_exc.py test/test_config.py test/test_util.py -k 'not test_actor_get_uid_laziness_not_called'
```

Scope note: The scope is the command below, not all optional upstream integrations.

Observed baseline (2026-11-10): `207 passed, 70 skipped, 1 deselected in 0.30s`
Whole-command wall time: 0.458 seconds, exit code 0. This is one local measurement, not a runtime guarantee. The first install is excluded.

This seed adds preparation metadata only. Application sources and upstream tests are unchanged; there is no task solution in this commit.
