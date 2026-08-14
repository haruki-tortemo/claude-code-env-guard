"""Interpreter floor check — fail closed, loudly.

Why this file exists
--------------------
This hook is about *guards that stop working without telling you*. The most
embarrassing version of that is the guard itself: run it on an interpreter that
cannot parse it, and it crashes on import. Claude Code reports a hook error,
the tool call proceeds, and you are unprotected — while your settings still say
the hook is installed.

We measured exactly that. The same `git add -A` was blocked under Python 3.14
(exit 2) and allowed under macOS's `/usr/bin/python3` 3.9.6 (exit 1, crash on a
`str | None` annotation). Nothing in the UI distinguished the two.

Two independent fixes, both required
------------------------------------
1. `from __future__ import annotations` in every hook module, so PEP 604
   annotations are lazy and the file parses back to 3.7. That removes the
   common cause.
2. This check, so a *future* edit that reintroduces too-new syntax cannot fail
   open silently.

Neither alone is enough: (1) does not stop the next person writing 3.10+ syntax,
and (2) alone would deny everything on a stock macOS Python.

Why exit 2 and not exit 1
-------------------------
Exit 1 means "hook error" and the operation still runs. For a guard, that is the
silent-failure mode this project exists to prevent. If we cannot guarantee the
check, we do not let the write through. The message says exactly how to fix it,
so this is a loud stop rather than a mystery.

Keep this module parseable by very old interpreters: no f-strings in the failure
path, no annotations, no walrus, no match. It has to survive being imported by
the interpreter it is about to reject.
"""

import sys

# macOS ships 3.9.6 as /usr/bin/python3. Supporting it is a distribution
# requirement, not a nicety: hooks are launched via PATH resolution, and PATH
# differs between an interactive shell, launchd, and cron.
MIN_VERSION = (3, 9)

BLOCK_EXIT = 2

_MESSAGE = """rc-env-guard: refusing to run on Python %s (needs %s or newer).

This is a hard stop, not a warning. A guard that cannot run must not report
success -- that is the exact failure this hook exists to prevent.

Interpreter in use: %s

Fix it in one of these ways, then retry:
  1. Point the hook at a newer interpreter in ~/.claude/settings.json, e.g.
     change   "command": "python3 .../rc_env_guard.py"
     to       "command": "/opt/homebrew/bin/python3 .../rc_env_guard.py"
  2. Install a newer Python and make sure it comes first on PATH.

If you would rather run unprotected for now, remove the hook entry from
settings.json. Do that deliberately -- do not leave a guard installed that
cannot run.
"""


def _fmt(version_info):
    return "%d.%d.%d" % (version_info[0], version_info[1], version_info[2])


def require_minimum():
    """Exit 2 with an actionable message if the interpreter is too old.

    Call this at the top of every hook entry point, before anything else runs.
    """
    if sys.version_info[:2] >= MIN_VERSION:
        return
    sys.stderr.write(_MESSAGE % (
        _fmt(sys.version_info),
        "%d.%d" % MIN_VERSION,
        sys.executable,
    ))
    sys.exit(BLOCK_EXIT)
