---
name: container-build-and-test
description: Build and run HAL inside the halbuild container, the local bench for anything needing hal_py -- there is no Windows build. Use before running tools/hal_* scripts, hal_py tests, or any command that touches a built HAL.
---

# Container build and test -- the halbuild bench

## When to use
- Anything that imports `hal_py` or runs the `hal` binary, on a Windows
  checkout. HAL builds Linux/macOS only -- `install_dependencies.sh` has no
  Windows branch.
- Before trusting a local result: CI (GitHub Actions `ubuntu*.yml`) is the
  compile-and-test *authority*. Note that only `ubuntu22.04`, `ubuntu24.04` and
  `arm64` can be triggered with `workflow_dispatch` -- see below. The
  container is a fast local bench for iterating, not a substitute for a green
  CI run.

## Quickstart

```bash
# repo is mounted at /work (a full HAL checkout), a Debug ninja build lives
# at /work/build. Every hal_py-touching command needs these three:
docker exec -e HAL_BASE_PATH=/work/build -e HAL_PY_PATH=/work/build/lib \
    -e PYTHONPATH=/work/build/lib -w /work halbuild bash -c \
    'python3 tools/hal_viz module_tree examples/fsm -o /tmp/out/'
```

Use `python3` explicitly, not `python` -- the container's `python` is a
symlink someone added by hand (`/usr/local/bin/python -> /usr/bin/python3`),
not a base-image guarantee; don't depend on it existing in a fresh container.

## Syncing sources from Windows

Two independent mechanisms, don't confuse them:

**One-off file(s), from Git Bash:** `docker cp` takes local paths verbatim --
`MSYS_NO_PATHCONV` must be **unset** or the copy silently mangles the path.
`docker exec` is the opposite: its arguments are container-side paths, and
Git Bash's automatic `/`-path conversion can corrupt them, so set
`MSYS_NO_PATHCONV=1` *only* for `exec`, never for `cp`.

```bash
docker cp "D:/hal/tools/hal_fsm/candidates.py" halbuild:/work/tools/hal_fsm/candidates.py
MSYS_NO_PATHCONV=1 docker exec halbuild bash -c 'ls /work/tools/hal_fsm'
```

**A full sync to a given ref:** `D:\hal` is bind-mounted at `/src` inside the
container (`git remote -v` in `/work` shows `origin -> /src`), but `/src`'s
working tree has CRLF line endings, which break shell scripts run from
there (`run_all.sh` et al. expect LF). Never run analysis from `/src` --
fetch from it into `/work` instead:

```bash
docker exec halbuild bash -c \
    'cd /work && git fetch /src <ref> && git checkout FETCH_HEAD'
```

`<ref>` is a branch, tag, or commit in the Windows checkout. This updates
`/work`'s LF-clean tree without pulling CRLF into it.

## Rebuilding after a sync

```bash
docker exec halbuild bash -c 'cd /work/build && ninja'
```

Always `ninja` in `/work/build` after any checkout that touched C++, a
plugin, or `hal_py`'s bindings -- even if the change looks Python-only.
**A stale `.so` produces a phantom segfault with no obvious cause.** If a
crash makes no sense given the source you're looking at, compare `.so` mtime
against source mtime before debugging the crash itself:

```bash
docker exec halbuild bash -c \
    'ls -la /work/build/lib/hal_py.so; find /work/src -newer /work/build/lib/hal_py.so'
```

If anything under `/work/src` is newer than `hal_py.so`, rebuild first.

## Filtering HAL's log noise

HAL's native log lines print to stdout at `[info]`/`[warning]` and drown out
a script's own output:

```bash
... 2>&1 | grep -vE "\[info\]|\[warning\]"
```

## CI escalation (do not skip this)

Per this repo's `CLAUDE.md`: if a CI/CD run fails **more than twice** for the
same piece of work (the third consecutive failure), stop autonomous fix
attempts and return to the main session/Fable for review before continuing --
include what failed each run (log excerpts), what changed between attempts
and why, and the current hypothesis. A red build is ~20-50 minutes; repeated
cycles on a wrong diagnosis are the expensive failure mode this guards
against, not the CI minutes themselves.

## Where things live
- `install_dependencies.sh` -- Linux/macOS/Ubuntu-apt/Arch-yay/RHEL paths
  only, no Windows branch.
- `Dockerfile` / `docker-compose.yml` at repo root -- the checked-in image
  definition (builds at `/hal`, not `/work`); the running `halbuild` bench
  container may be provisioned differently (`/work` + `/work/build`) -- treat
  the container's actual layout, not the Dockerfile, as ground truth for
  paths, and confirm with `docker ps`/`docker exec ... ls` before assuming
  either.
- `.github/workflows/ubuntu22.04.yml`, `ubuntu24.04.yml`, `ubuntu26.04.yml`,
  `arm64.yml`, `macOS.yml` -- the CI authority. Only `ubuntu22.04.yml`,
  `ubuntu24.04.yml` and `arm64.yml` declare `workflow_dispatch`; `ubuntu26.04.yml`,
  `macOS.yml` and `releaseDoc.yml` run on push/PR only, so there is no way to
  dispatch them by hand -- to exercise those three you must push a branch or
  open a PR.
- Build flags CI uses: `-DBUILD_ALL_PLUGINS=ON -DBUILD_TESTS=ON` (Debug for
  CI's own build; the bench's `/work/build` is also Debug).
