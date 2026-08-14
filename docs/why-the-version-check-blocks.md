# Why the version check blocks instead of warning

`rc_env_guard.py` opens with an interpreter floor:

```python
MIN_PYTHON = (3, 9)
if sys.version_info[:2] < MIN_PYTHON:
    sys.stderr.write("rc-env-guard: refusing to run ...")
    raise SystemExit(2)
```

Exit 2 is the **blocking** exit code for a Claude Code hook. On a too-old
interpreter, this guard does not warn, does not degrade, does not step aside.
It stops the operation. This document explains why that is the only honest
choice.

## The failure we measured

This project exists because of guards that stop working without telling anyone.
The most embarrassing version of that is the guard itself.

Run a hook on an interpreter that cannot parse it and it crashes on import.
Claude Code reports a hook *error* (exit 1) — and **the tool call proceeds**.
You are unprotected, while your `settings.json` still says the hook is
installed.

We measured exactly this failure mode during development:

- the same blocked operation was **denied** under Python 3.14 (exit 2)
- and **allowed** under macOS's stock `/usr/bin/python3` 3.9.6
  (exit 1 — a crash on a `str | None` annotation, which is 3.10+ syntax).

Nothing in the UI distinguished the two runs. The guard's protection depended
on which `python3` happened to be first on PATH — and PATH differs between an
interactive shell, launchd, and cron.

## Why exit 2 and not exit 1

Exit 1 means "hook error, operation continues". For an ordinary hook that is a
reasonable degraded mode. For a *guard*, it is precisely the silent-failure
mode the guard exists to prevent:

> **A guard that cannot run must not report success — and for a guard,
> "the operation proceeded" is indistinguishable from success.**

If the check cannot run, we do not let the write through. The refusal message
states the interpreter in use and the exact ways to fix it, so this is a loud,
actionable stop rather than a mystery.

## Three guards, three different failure modes

1. **`from __future__ import annotations`** keeps the current annotations
   (`str | None`) compatible with Python 3.9.
2. **The runtime version check** gives a clear, blocking refusal — but only
   after the interpreter has parsed the module. It **cannot** protect against
   syntax that Python 3.9 cannot parse: the interpreter parses the whole file
   before executing the first line, so 3.10+ syntax anywhere in the module
   crashes before the check ever runs.
3. **The Python 3.9 CI job** (`.github/workflows/test.yml`) is the guard for
   that parse-level failure mode: it runs all three test suites on a real
   3.9 interpreter, so a release with incompatible syntax cannot ship green.

All three are required: CI prevents incompatible releases, while the runtime
check gives users an actionable error when they invoke a compatible release
with an unsupported interpreter.

## Why the check is inlined in the hook

`hooks/_compat.py` is the reference implementation of this check, with the
same rationale in its docstring. The hook does **not** import it — the check
is copied inline at the top of `rc_env_guard.py`, on purpose:

- A hook must be distributable as a **single file**. Claude Code launches it
  by path; requiring a sibling module breaks a one-file install.
- Our own mutation tests copy the hook to another directory and run it there.
  With a sibling import, those tests stop working — we found this by breaking
  them.

The inline block must stay parseable by the very interpreter it is about to
reject: no f-strings in the failure path, no annotations, no walrus, no
`match` before the `SystemExit`.

## Why 3.9 is the floor

macOS ships 3.9.6 as `/usr/bin/python3`. Supporting it is a distribution
requirement, not a nicety: hooks are launched via PATH resolution, and on a
machine with no Homebrew/pyenv Python, the stock interpreter is what runs.
The floor is the oldest interpreter CI runs the full suites on;
`tests/test_compat.py` exercises the refusal contract itself, fault injection
included.

## If the check fires on your machine

The refusal message says the same, but for completeness:

1. Point the hook at a newer interpreter in `~/.claude/settings.json` —
   change `"command": "python3 .../rc_env_guard.py"` to an absolute path such
   as `"/opt/homebrew/bin/python3 .../rc_env_guard.py"`, **or**
2. install a newer Python and put it first on PATH.

If you would rather run unprotected, remove the hook entry from
`settings.json` — deliberately. Do not leave a guard installed that cannot
run.
