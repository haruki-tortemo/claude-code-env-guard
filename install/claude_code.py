#!/usr/bin/env python3
"""Install rc-env-guard into ~/.claude/settings.json by merging, never replacing.

Design rules, in order of priority:

  1. Never destroy a settings file we do not fully understand.
     Unreadable, missing-parent, or invalid JSON -> stop and report. Do not
     "helpfully" write a fresh file: the user's hooks, permissions and env would
     be gone, and the failure would look like a successful install.
  2. Back up before touching anything, with a timestamp so repeated runs do not
     clobber each other's backups.
  3. Be idempotent. Running twice must not create two entries.
  4. Write atomically. A crash mid-write must not leave a truncated settings.json
     -- that would disable every hook the user has, silently.
  5. Say exactly what changed.

Usage:
    python3 install/claude_code.py [--dry-run] [--uninstall] [--settings PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

if sys.version_info[:2] < (3, 9):
    sys.stderr.write("rc-env-guard installer needs Python 3.9 or newer.\n")
    raise SystemExit(2)

HOOK_FILE = Path(__file__).resolve().parents[1] / "hooks" / "rc_env_guard.py"
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"

# All three are required. See README "How it decides": a variable can arrive
# through a path PreToolUse never sees, and a registration that lost an event is
# a guard that looks installed and is not.
REGISTRATIONS = (
    ("PreToolUse", "Edit|Write|MultiEdit|Bash", 10, "Checking for RC-killing env vars"),
    ("SessionStart", "startup|resume|clear|compact|fork", 10, None),
    ("ConfigChange", "user_settings|project_settings|local_settings|policy_settings", 10, None),
)


class Abort(Exception):
    """Stop without writing. The message is for the user, not a stack trace."""


def load_settings(path: Path) -> dict:
    if not path.exists():
        raise Abort(
            "%s does not exist.\n"
            "Start Claude Code once so it creates your settings file, then re-run.\n"
            "(We will not create it for you -- a settings file we invented could\n"
            "silently replace configuration you expected to be there.)" % path
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Abort("Cannot read %s (%s). Nothing was changed." % (path, exc.__class__.__name__))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Abort(
            "%s is not valid JSON (line %d, column %d: %s).\n"
            "Nothing was changed. Fix the file by hand, then re-run.\n"
            "We do not rewrite a settings file we cannot parse." % (
                path, exc.lineno, exc.colno, exc.msg)
        )
    if not isinstance(data, dict):
        raise Abort("%s does not contain a JSON object. Nothing was changed." % path)
    return data


def guard_command() -> str:
    return "python3 %s" % HOOK_FILE


def entry_matches(entry: dict) -> bool:
    """Is this hook entry ours? Compare the resolved script path, not a substring."""
    for hook in entry.get("hooks", []) or []:
        command = hook.get("command")
        if not isinstance(command, str):
            continue
        for token in command.split():
            if token.endswith(HOOK_FILE.name):
                try:
                    if Path(token).expanduser().resolve() == HOOK_FILE:
                        return True
                except OSError:
                    continue
    return False


def install(data: dict) -> list[str]:
    changes: list[str] = []
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise Abort('"hooks" in settings.json is not an object. Nothing was changed.')

    for event, matcher, timeout, status in REGISTRATIONS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise Abort('"hooks.%s" is not a list. Nothing was changed.' % event)
        if any(entry_matches(e) for e in entries if isinstance(e, dict)):
            continue
        handler = {"type": "command", "command": guard_command(), "timeout": timeout}
        if status:
            handler["statusMessage"] = status
        entries.append({"matcher": matcher, "hooks": [handler]})
        changes.append("added %s (matcher: %s)" % (event, matcher))
    return changes


def uninstall(data: dict) -> list[str]:
    changes: list[str] = []
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return changes
    for event in list(hooks):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not (isinstance(e, dict) and entry_matches(e))]
        if len(kept) != len(entries):
            changes.append("removed %d entr%s from %s"
                           % (len(entries) - len(kept),
                              "y" if len(entries) - len(kept) == 1 else "ies", event))
            hooks[event] = kept
    return changes


def write_atomically(path: Path, data: dict) -> Path:
    backup = path.with_suffix(".json.backup-%s" % time.strftime("%Y%m%dT%H%M%S"))
    shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)          # atomic on POSIX
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and exit without writing")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    args = parser.parse_args()

    if not HOOK_FILE.exists():
        print("Hook not found at %s -- run this from a checkout of the repo." % HOOK_FILE,
              file=sys.stderr)
        return 1

    try:
        data = load_settings(args.settings)
        changes = uninstall(data) if args.uninstall else install(data)
    except Abort as exc:
        print(str(exc), file=sys.stderr)
        return 1

    verb = "uninstall" if args.uninstall else "install"
    if not changes:
        print("Nothing to do -- rc-env-guard is already %sed in %s."
              % ("uninstall" if args.uninstall else "install", args.settings))
        return 0

    if args.dry_run:
        print("Would %s in %s:" % (verb, args.settings))
        for change in changes:
            print("  - %s" % change)
        print("\nNo changes written (--dry-run).")
        return 0

    backup = write_atomically(args.settings, data)
    print("%sed rc-env-guard in %s:" % (verb.capitalize(), args.settings))
    for change in changes:
        print("  - %s" % change)
    print("\nBackup: %s" % backup)
    print("Restart Claude Code for the change to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
