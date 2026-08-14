#!/usr/bin/env python3
"""Contract tests for the interpreter floor check.

A guard's own guard needs tests, or it is decoration. These assert the two
behaviors that matter:

  - too old  -> exit 2 (block), with an actionable message on stderr
  - new enough -> returns, silently

The "too old" case is exercised by faking sys.version_info, so the test runs on
any interpreter rather than requiring an ancient one to be installed.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

HOOKS = pathlib.Path(__file__).resolve().parents[1] / "hooks"

# Fake the version in a subprocess so the real interpreter is irrelevant.
DRIVER = """
import sys
sys.path.insert(0, {hooks!r})
import _compat
_compat.sys = type("S", (), {{
    "version_info": {version!r},
    "executable": "/usr/bin/python3",
    "stderr": sys.stderr,
    "exit": sys.exit,
}})
_compat.require_minimum()
print("RETURNED")
"""


def run(version: tuple) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER.format(hooks=str(HOOKS), version=version)],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


CASES = [
    # (label, version, expected exit, must appear in output)
    ("3.8.10 is below the floor", (3, 8, 10), 2, "refusing to run"),
    ("3.9.6 (macOS system) is allowed", (3, 9, 6), 0, "RETURNED"),
    ("3.9.0 exactly at the floor", (3, 9, 0), 0, "RETURNED"),
    ("3.14.6 is allowed", (3, 14, 6), 0, "RETURNED"),
    ("2.7.18 is below the floor", (2, 7, 18), 2, "refusing to run"),
]

EXTRA = [
    # The message has to be actionable, not just a refusal.
    ("names the interpreter in use", (3, 8, 10), "/usr/bin/python3"),
    ("tells you how to fix it", (3, 8, 10), "settings.json"),
    ("says it is deliberate, not a warning", (3, 8, 10), "hard stop"),
]


def main() -> int:
    failures = []

    for label, version, want_exit, want_text in CASES:
        code, out = run(version)
        ok = code == want_exit and want_text in out
        print(("PASS " if ok else "FAIL ") + label + " (exit=%d)" % code)
        if not ok:
            failures.append(label)

    for label, version, want_text in EXTRA:
        _, out = run(version)
        ok = want_text in out
        print(("PASS " if ok else "FAIL ") + "message " + label)
        if not ok:
            failures.append(label)

    # Fault injection: if the check is neutered, the "too old" case must go red.
    neutered = DRIVER.replace("_compat.require_minimum()", "pass")
    proc = subprocess.run(
        [sys.executable, "-c", neutered.format(hooks=str(HOOKS), version=(3, 8, 10))],
        capture_output=True, text=True,
    )
    detected = proc.returncode != 2
    print(("PASS " if detected else "FAIL ")
          + "fault injection: removing the check makes the block disappear")
    if not detected:
        failures.append("fault injection")

    print()
    if failures:
        print("FAILED: %d" % len(failures))
        return 1
    print("%d checks passed" % (len(CASES) + len(EXTRA) + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
