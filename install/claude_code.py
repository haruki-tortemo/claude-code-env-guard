#!/usr/bin/env python3
"""Install rc-env-guard into Claude Code settings by merging, never replacing.

What installing does:

  1. Copies `hooks/rc_env_guard.py` to a stable location next to the settings
     file (`<settings dir>/hooks/rc_env_guard.py`) and registers THAT copy.
     The checkout can be moved or deleted afterwards - the registration does
     not point into it. The path in the registered command is shell-quoted, so
     a path containing spaces still launches.
  2. Merges three hook registrations into the settings file. Existing hooks,
     permissions and env are preserved.

Design rules, in order of priority:

  1. Never destroy a settings file we do not fully understand.
     Unreadable, missing-parent, or invalid JSON -> stop and report. Do not
     "helpfully" write a fresh file: the user's hooks, permissions and env would
     be gone, and the failure would look like a successful install.
  2. Refuse targets the guard does not watch. The hook only inspects
     `.claude/settings.json` and `.claude/settings.local.json`; installing into
     any other path would report success while guarding nothing.
  3. Back up before touching anything. Backup names are created exclusively,
     so two runs in the same second cannot clobber each other's backups.
  4. Be idempotent - running twice must not create two entries - and repair:
     a registration that matches our script but lost an event, gained
     `async: true`, or had its matcher narrowed is replaced with a correct
     one (found in review: a half-broken registration used to be accepted as
     "already installed", leaving the guard silently disarmed).
  5. Write atomically. A crash mid-write must not leave a truncated
     settings.json - that would disable every hook the user has, silently.
  6. Say exactly what changed.

Usage:
    python3 install/claude_code.py [--dry-run] [--uninstall] [--settings PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

if sys.version_info[:2] < (3, 9):
    sys.stderr.write("rc-env-guard installer needs Python 3.9 or newer.\n")
    raise SystemExit(2)

HOOK_SOURCE = Path(__file__).resolve().parents[1] / "hooks" / "rc_env_guard.py"
HOOK_NAME = "rc_env_guard.py"
DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"

# Must mirror the hook's own SETTINGS_RE: installing into a path the hook never
# inspects would succeed and guard nothing (found in review: `--settings
# /tmp/custom.json` exited 0, then a write to that same file passed the guard).
SETTINGS_RE = re.compile(r"(?:^|/)\.claude/settings(?:\.local)?\.json$")

# These two must stay in sync with the hook's registration contract; the test
# suite pins them by running the installed copy's registration_problem() over
# the settings the installer produced.
SHELL_CONTROL = re.compile(r"\|\||&&|[;&|]|\$\(|`|>>?|<|\n")
INTERPRETERS = {"python", "python3", "python3.11", "python3.12", "python3.13"}

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


def hook_destination(settings: Path) -> Path:
    return settings.parent / "hooks" / HOOK_NAME


def guard_command(dest: Path) -> str:
    return "python3 %s" % shlex.quote(str(dest))


def matcher_tokens(matcher) -> set:
    return {t.strip() for t in re.split(r"[|,]", str(matcher or "")) if t.strip()}


def command_argv(command):
    if not isinstance(command, str) or SHELL_CONTROL.search(command):
        return None
    try:
        return shlex.split(command, comments=True)
    except ValueError:
        return None


def entry_is_ours(entry: dict) -> bool:
    """Does any handler in this entry reference our script by basename?

    Deliberately loose: uninstall and repair must also catch stale or broken
    registrations (old paths, missing quoting) that a strict check would skip.
    """
    for hook in entry.get("hooks", []) or []:
        command = hook.get("command")
        if not isinstance(command, str):
            continue
        argv = command_argv(command)
        tokens = argv if argv is not None else command.split()
        for token in tokens:
            if token.split("/")[-1] == HOOK_NAME or token.endswith("/" + HOOK_NAME):
                return True
    return False


def entry_is_healthy(entry: dict, needed: str, dest: Path) -> bool:
    """Is this entry a fully working registration of the installed copy?

    Mirrors the hook's own structural invariant: matcher as a token superset,
    handler type `command`, not `async` (async hooks cannot block), no shell
    control in the command, and the argv target resolving to the installed
    hook file. Found in review: accepting "the script path appears somewhere"
    let a `matcher: Bash` + `async: true` registration pass as installed.
    """
    if not isinstance(entry, dict) or "if" in entry:
        return False
    if not matcher_tokens(entry.get("matcher")) >= matcher_tokens(needed):
        return False
    for hook in entry.get("hooks", []) or []:
        if not isinstance(hook, dict):
            continue
        if hook.get("type") != "command" or hook.get("async") is True:
            continue
        argv = command_argv(hook.get("command"))
        if not argv:
            continue
        if len(argv) == 1:
            target = argv[0]
        elif len(argv) == 2 and argv[0].split("/")[-1] in INTERPRETERS:
            target = argv[1]
        else:
            continue
        try:
            if Path(os.path.expanduser(target)).resolve() == dest.resolve():
                return True
        except OSError:
            continue
    return False


def install(data: dict, dest: Path) -> list:
    changes = []
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise Abort('"hooks" in settings.json is not an object. Nothing was changed.')

    for event, matcher, timeout, status in REGISTRATIONS:
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise Abort('"hooks.%s" is not a list. Nothing was changed.' % event)
        ours = [e for e in entries if isinstance(e, dict) and entry_is_ours(e)]
        if any(entry_is_healthy(e, matcher, dest) for e in ours):
            continue
        for stale in ours:
            entries.remove(stale)
        handler = {"type": "command", "command": guard_command(dest), "timeout": timeout}
        if status:
            handler["statusMessage"] = status
        entries.append({"matcher": matcher, "hooks": [handler]})
        verb = "repaired" if ours else "added"
        changes.append("%s %s (matcher: %s)" % (verb, event, matcher))
    return changes


def uninstall(data: dict) -> list:
    changes = []
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return changes
    for event in list(hooks):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not (isinstance(e, dict) and entry_is_ours(e))]
        if len(kept) != len(entries):
            changes.append("removed %d entr%s from %s"
                           % (len(entries) - len(kept),
                              "y" if len(entries) - len(kept) == 1 else "ies", event))
            hooks[event] = kept
    return changes


def hook_copy_change(dest: Path):
    """What copying the hook would change, or None if already current."""
    source_bytes = HOOK_SOURCE.read_bytes()
    if dest.exists():
        try:
            if dest.read_bytes() == source_bytes:
                return None
        except OSError:
            pass
        return "update hook file at %s" % dest
    return "install hook file at %s" % dest


def copy_hook(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".rc-env-guard-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(HOOK_SOURCE.read_bytes())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def unique_backup_path(path: Path) -> Path:
    """Create the backup file exclusively so a same-second rerun cannot reuse
    (and overwrite) an earlier backup's name."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    n = 0
    while True:
        suffix = ".json.backup-%s" % stamp if n == 0 else ".json.backup-%s-%d" % (stamp, n)
        candidate = path.with_suffix(suffix)
        try:
            os.close(os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return candidate
        except FileExistsError:
            n += 1


def write_atomically(path: Path, data: dict) -> Path:
    backup = unique_backup_path(path)
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

    if not SETTINGS_RE.search(args.settings.as_posix()):
        print(
            "Refusing target %s.\n"
            "The guard only inspects .claude/settings.json and "
            ".claude/settings.local.json;\ninstalling anywhere else would "
            "report success while guarding nothing." % args.settings,
            file=sys.stderr)
        return 1

    if not args.uninstall and not HOOK_SOURCE.exists():
        print("Hook not found at %s -- run this from a checkout of the repo." % HOOK_SOURCE,
              file=sys.stderr)
        return 1

    dest = hook_destination(args.settings)

    try:
        data = load_settings(args.settings)
        if args.uninstall:
            changes = uninstall(data)
            if dest.exists():
                changes.append("remove hook file %s" % dest)
            copy_change = None
        else:
            changes = install(data, dest)
            copy_change = hook_copy_change(dest)
            if copy_change:
                changes.append(copy_change)
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

    backup = None
    if args.uninstall:
        settings_changes = [c for c in changes if not c.startswith("remove hook file")]
        if settings_changes:
            backup = write_atomically(args.settings, data)
        if dest.exists():
            try:
                os.remove(str(dest))
            except OSError as exc:
                print("Warning: could not remove %s (%s) -- remove it by hand."
                      % (dest, exc.__class__.__name__), file=sys.stderr)
    else:
        # Copy the hook first: the registration must never point at a missing file.
        if copy_change:
            copy_hook(dest)
        settings_changes = [c for c in changes if c is not copy_change]
        if settings_changes:
            backup = write_atomically(args.settings, data)

    print("%sed rc-env-guard in %s:" % (verb.capitalize(), args.settings))
    for change in changes:
        print("  - %s" % change)
    if backup:
        print("\nBackup: %s" % backup)
    print("Restart Claude Code for the change to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
