#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Haruki Yoshino
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
import stat
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


def _shown(value) -> str:
    """`value` for a terminal: non-printable characters escaped, so a crafted
    file or link name cannot rewrite or hide the message printed around it."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii")
                   for c in str(value))


def _strictly_inside(root: str, path: str) -> bool:
    try:
        return path != root and os.path.commonpath([root, path]) == root
    except ValueError:                  # e.g. different drives on Windows
        return False


def _in_work_tree(real_dir: str) -> bool:
    """Is this resolved directory inside a git work tree (it, or a directory
    above it, has a `.git` entry)? Anything in a work tree, symlinks included,
    can be committed repository content."""
    d = real_dir
    while True:
        if os.path.lexists(os.path.join(d, ".git")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def _same_dir(a: str, b: str) -> bool:
    """Are `a` and `b` one directory on disk? File identity, not spelling: on a
    case-insensitive volume `.CLAUDE` is `.claude`, and on macOS
    /System/Volumes/Data/Users/... is /Users/..., yet the strings differ."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def working_dir() -> str:
    """The working directory as the shell names it.

    os.getcwd() is the physical path: if the user reached the directory with
    `cd` through a symlink, the link is already gone from it, so a relative
    --settings would hide that link from project_write_root. The shell's $PWD
    keeps it. Trust $PWD only when it is absolute and names the same directory.
    """
    cwd = os.getcwd()
    pwd = os.environ.get("PWD", "")
    if os.path.isabs(pwd) and _same_dir(pwd, cwd):
        return os.path.normpath(pwd)
    return cwd


def project_write_root(settings: Path):
    """The real directory a project-level install may write in, or None for an
    install into the user's own ~/.claude.

    A project's `.claude/` is repository content, and git checks committed
    symlinks out as symlinks. Found in review: a symlink committed at the hook
    file, at `hooks/`, at `.claude`, at `settings.json` (to the user's
    ~/.claude/settings.json) or at a directory above `.claude` let a project
    install write files the user never named - including merging our
    registration, pointing into the repository, into the user's GLOBAL
    settings. A link in an unpacked archive (`.claude -> ../<another
    clone>/.claude`) retargets the install just the same, and nothing on disk
    tells such a link apart from one the user made. So for any path other than
    ~/.claude named as such, fail safe:

      - a symlink anywhere in the named directory path is refused - inside a
        git work tree (it may have been committed) and outside one (it cannot
        be told apart from an archive's), whoever owns it. Only the filesystem
        root's own links (/tmp, /var on macOS) are followed. The refusal names
        the resolved path, which the user can pass instead;
      - a path that is the user's ~/.claude by file identity but named another
        way (another letter case, the /System/Volumes/Data firmlink, the real
        directory behind a dotfiles link) is refused;
      - the hook file itself must not be a symlink, even to a file inside the
        directory: a committed `hooks/rc_env_guard.py -> ../settings.local.json`
        would have the hook copy overwrite the user's local settings;
      - every file written or removed must resolve inside that directory
        (require_inside). A settings-file or `hooks/` link that stays inside
        it keeps working.

    ~/.claude itself keeps following every symlink: it is the user's own, and
    dotfiles-managed links there are expected.
    """
    named_dir = Path(working_dir(), settings.parent)
    home_claude = os.path.abspath(str(DEFAULT_SETTINGS.parent))
    home_real = os.path.realpath(home_claude)
    real_dir = os.path.realpath(str(named_dir))
    own = real_dir == home_real or _same_dir(real_dir, home_real)
    if own and os.path.abspath(str(named_dir)) in (
            home_claude,
            os.path.join(os.path.realpath(os.path.dirname(home_claude)),
                         os.path.basename(home_claude))):
        return None
    suggest = os.path.join(home_claude if own else real_dir, settings.name)
    probe = Path(named_dir.parts[0])
    for part in named_dir.parts[1:]:
        holder = os.path.realpath(str(probe))      # the real directory the entry is in
        probe = probe / part
        try:
            st = os.lstat(str(probe))
        except OSError:
            break
        if not stat.S_ISLNK(st.st_mode) or holder == os.path.dirname(holder):
            continue                    # not a link, or the system's own (/tmp, /var)
        if _in_work_tree(holder):
            why = ("it is inside a git work tree, so the repository may have committed it;\n"
                   "following it could send the install anywhere, even into your user settings")
        else:
            why = ("it is outside any git work tree, where a link of yours cannot be told\n"
                   "apart from one unpacked from an archive")
        raise Abort(
            "%s is a symlink (to %s).\n"
            "Refusing to follow it: %s.\n"
            "If %s is where you mean to install, pass that path instead.\n"
            "Nothing was changed." % (
                _shown(probe), _shown(os.readlink(str(probe))), why, _shown(suggest)))
    if own:
        raise Abort(
            "%s is your user settings directory %s, named another way.\n"
            "Refusing to install there as if it were a project's.\n"
            "If %s is where you mean to install, pass that path instead.\n"
            "Nothing was changed." % (
                _shown(named_dir), _shown(home_claude), _shown(suggest)))
    hook = hook_destination(settings)
    if os.path.islink(str(hook)):
        raise Abort(
            "%s is a symlink (to %s).\n"
            "Refusing to write the hook through it: in a project's settings directory\n"
            "a link the project carries can aim it at another file, such as\n"
            "settings.local.json.\n"
            "Remove the link if the hook belongs at that path.\n"
            "Nothing was changed." % (_shown(hook), _shown(os.readlink(str(hook)))))
    return real_dir


def require_inside(root, path: Path, follow: bool = True) -> Path:
    """`path` with symlinks resolved - all but its last component when
    `follow` is false, which is where removing it acts. For a project install
    (`root` set), refuse unless that lies inside the project's settings
    directory."""
    real = (os.path.realpath(str(path)) if follow else
            os.path.join(os.path.realpath(str(path.parent)), path.name))
    if root is not None and not _strictly_inside(root, real):
        raise Abort(
            "%s resolves to %s, outside the settings directory %s.\n"
            "Refusing to follow a symlink out of a project's settings directory."
            % (_shown(path), _shown(real), _shown(root)))
    return Path(real)


def guard_command(dest: Path) -> str:
    return "python3 %s" % shlex.quote(str(dest))


def matcher_tokens(matcher) -> set:
    return {t.strip() for t in re.split(r"[|,]", str(matcher or "")) if t.strip()}


_QUOTED_SPAN = re.compile(r"'[^']*'|\"[^\"]*\"")


def has_shell_control(command: str) -> bool:
    """Shell control/expansion OUTSIDE any quoted span.

    Found in review: screening the raw command string rejected a perfectly safe
    `shlex.quote`d path that merely contains `&`/`;`/`|` (a directory named
    `R&D`), so the installer could not recognize its own registration. Quoted
    spans carry no shell meaning, so blank them out before testing.
    """
    return bool(SHELL_CONTROL.search(_QUOTED_SPAN.sub("", command)))


def command_argv(command):
    if not isinstance(command, str) or has_shell_control(command):
        return None
    try:
        return shlex.split(command, comments=True)
    except ValueError:
        return None


def _resolve(token) -> str:
    try:
        return os.path.realpath(os.path.expanduser(str(token)))
    except OSError:
        return ""


def execution_target(command) -> str:
    """The single script this command would EXECUTE, or None if the form is not
    a plain `<script>` / `<interpreter> <script>`.

    Mirrors the hook's own command_runs_guard extraction so the two agree.
    Found in review: comparing *any* argv token let `echo <our path>` (which
    merely passes our path as an argument, never executing it) be treated as
    ours and deleted. Only the execution target counts; anything else is
    ambiguous and left untouched, matching the "do not break other hooks"
    priority.
    """
    argv = command_argv(command)
    if not argv:
        return None
    if len(argv) == 1:
        return argv[0]
    if len(argv) == 2 and os.path.basename(argv[0]) in INTERPRETERS:
        return argv[1]
    return None


def target_is_guard(target, dest: Path) -> bool:
    """The single condition, shared by ownership and health checks and matching
    the hook's own command_runs_guard: the execution target has our basename
    AND resolves to the installed copy. The basename half matters so the
    installer and the hook agree - a differently-named symlink that resolves to
    our file is a registration the hook does NOT recognize, so the installer
    must not treat it as one either."""
    return (target is not None
            and os.path.basename(target) == HOOK_NAME
            and _resolve(target) == _resolve(dest))


def handler_is_ours(handler, dest: Path) -> bool:
    """Does this single handler EXECUTE our installed copy?

    A different path with the same basename, a differently-named symlink, or a
    command that merely mentions our path as an argument is not ours. Defensive
    against non-dict handlers.
    """
    if not isinstance(handler, dict):
        return False
    return target_is_guard(execution_target(handler.get("command")), dest)


def foreign_same_basename_paths(data: dict, dest: Path) -> list:
    """Handlers that EXECUTE our script's basename from a different path.

    We do NOT touch these (a legitimate third-party hook, or a decoy). We report
    them so the operator can decide - the tool's whole point is to not be silent.
    """
    dest_real = _resolve(dest)
    found = []
    hooks = (data or {}).get("hooks")
    if not isinstance(hooks, dict):
        return found
    for entries in hooks.values():
        for e in entries if isinstance(entries, list) else []:
            handlers = e.get("hooks") if isinstance(e, dict) else None
            for h in handlers if isinstance(handlers, list) else []:
                command = h.get("command") if isinstance(h, dict) else None
                target = execution_target(command)
                if (target is not None
                        and os.path.basename(target) == HOOK_NAME
                        and _resolve(target) != dest_real):
                    found.append(target)
    return sorted(set(found))


def entry_is_ours(entry: dict, dest: Path) -> bool:
    """Does any handler in this entry reference our installed copy?"""
    if not isinstance(entry, dict):
        return False
    handlers = entry.get("hooks")
    return isinstance(handlers, list) and any(handler_is_ours(h, dest) for h in handlers)


def strip_our_handlers(entries: list, dest: Path):
    """Return (new_entries, removed_count), removing only OUR handlers.

    Handler granularity, not entry granularity: a settings entry that groups our
    handler together with the user's own hook keeps the user's hook. Found in
    review: entry-granularity removal silently deleted co-located third-party
    hooks and only reported a count.
    """
    new_entries = []
    removed = 0
    for e in entries:
        handlers = e.get("hooks") if isinstance(e, dict) else None
        if isinstance(handlers, list) and any(handler_is_ours(h, dest) for h in handlers):
            kept = [h for h in handlers if not handler_is_ours(h, dest)]
            removed += len(handlers) - len(kept)
            if kept:                       # other handlers remain -> keep the entry
                new_entries.append({**e, "hooks": kept})
            # else: entry held only our handler -> drop it
        else:
            new_entries.append(e)
    return new_entries, removed


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
        if target_is_guard(execution_target(hook.get("command")), dest):
            return True
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
        if any(isinstance(e, dict) and entry_is_healthy(e, matcher, dest) for e in entries):
            continue
        # Not healthy: strip only our (stale) handlers, keep co-located ones,
        # then append a correct fresh registration.
        new_entries, removed = strip_our_handlers(entries, dest)
        handler = {"type": "command", "command": guard_command(dest), "timeout": timeout}
        if status:
            handler["statusMessage"] = status
        new_entries.append({"matcher": matcher, "hooks": [handler]})
        hooks[event] = new_entries
        changes.append("%s %s (matcher: %s)"
                       % ("repaired" if removed else "added", event, matcher))
    return changes


def uninstall(data: dict, dest: Path) -> list:
    changes = []
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return changes
    for event in list(hooks):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        new_entries, removed = strip_our_handlers(entries, dest)
        if removed:
            changes.append("removed %d handler%s from %s"
                           % (removed, "" if removed == 1 else "s", event))
            hooks[event] = new_entries
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


def copy_hook(dest: Path, root: str = None) -> None:
    # Follow a symlink: write the real file so a dotfiles-managed hook path is
    # updated in place rather than replaced with a regular file. In a project
    # (`root` set) the hook file is never written through: project_write_root
    # refused a link there, and the entry itself is replaced, so one appearing
    # since is not followed either.
    real = require_inside(root, dest, follow=root is None)
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(real.parent), prefix=".rc-env-guard-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(HOOK_SOURCE.read_bytes())
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, real)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def unique_backup_open(path: Path):
    """Exclusively create the backup and return (fd, path).

    The fd is kept and written through, never reopened by name: a same-second
    rerun cannot reuse the name (O_EXCL), and there is no window in which the
    name could be swapped for a symlink between create and write.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S")
    n = 0
    while True:
        suffix = ".json.backup-%s" % stamp if n == 0 else ".json.backup-%s-%d" % (stamp, n)
        candidate = path.with_suffix(suffix)
        try:
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            return fd, candidate
        except FileExistsError:
            n += 1


def write_atomically(path: Path, data: dict, known_mtime_ns: int = None,
                     root: str = None) -> Path:
    # Operate on the real file so a symlinked settings.json is updated in place,
    # not converted to a regular file that detaches the user's dotfiles copy.
    # In a project (`root` set) only a link that stays in its settings
    # directory is followed.
    real = require_inside(root, path)

    # Open the source ONCE with O_NOFOLLOW and read the backup from that fd, so
    # the "resolve path, then read it by name" window cannot be raced (swapping
    # `real` for a symlink between the two). Identity/mode come from fstat on the
    # very fd we copy from.
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        src_fd = os.open(str(real), os.O_RDONLY | nofollow)
    except OSError as exc:
        raise Abort("Cannot open %s safely (%s). Nothing was changed." % (real, exc))
    try:
        src_stat = os.fstat(src_fd)
        orig_mode = src_stat.st_mode & 0o777
        # Backup: write through the exclusively-created fd, kept at 0600. Never
        # reopen by name, never copystat (no world-readable copy of a file that
        # may hold API keys).
        backup_fd, backup = unique_backup_open(real)
        with os.fdopen(backup_fd, "wb") as bf, os.fdopen(os.dup(src_fd), "rb") as src:
            shutil.copyfileobj(src, bf)
            bf.flush()
            os.fsync(bf.fileno())
    finally:
        os.close(src_fd)

    def _changed_under_us() -> bool:
        # Best-effort detection: a concurrent writer (Claude Code, a second
        # installer) or a symlink swap since we read the file. Non-cooperating
        # processes cannot be fully excluded without a shared lock.
        try:
            st = os.lstat(real)
        except OSError:
            return True
        if stat.S_ISLNK(st.st_mode):
            return True
        if (st.st_ino, st.st_dev) != (src_stat.st_ino, src_stat.st_dev):
            return True
        if known_mtime_ns is not None and st.st_mtime_ns != known_mtime_ns:
            return True
        return st.st_mtime_ns != src_stat.st_mtime_ns

    if _changed_under_us():
        raise Abort(
            "%s changed on disk while the installer was running.\n"
            "Nothing was written (a backup was taken at %s). Re-run to retry."
            % (real, backup))

    fd, tmp = tempfile.mkstemp(dir=str(real.parent), prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, orig_mode)        # keep the user's mode, don't silently change it
        # Re-check right before the swap: closes the window between the check
        # above and the replace.
        if _changed_under_us():
            os.unlink(tmp)
            raise Abort(
                "%s changed on disk just before the write.\n"
                "Nothing was written (a backup was taken at %s). Re-run to retry."
                % (real, backup))
        os.replace(tmp, real)           # atomic on POSIX
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

    # Normalize (collapse `//`, `/.`) before matching so the gate stays in
    # lockstep with the hook's own path check.
    settings_posix = Path(os.path.normpath(str(args.settings))).as_posix()
    if not SETTINGS_RE.search(settings_posix):
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

    # Work on the absolute path the user named (after the gate above, which
    # judges the argument as typed). A relative --settings would otherwise be
    # checked against the physical cwd (hiding a symlink the user `cd`ed
    # through) and registered as a relative command, which in user settings
    # would run each project's own .claude/hooks file.
    if not args.settings.is_absolute():
        args.settings = Path(working_dir(), args.settings)

    dest = hook_destination(args.settings)

    try:
        # Before reading anything, and in --dry-run too: no symlink a project
        # may carry (committed, or unpacked from an archive) can move a write,
        # or the uninstall's delete, out of the directory the user named, and
        # the hook copy is never written through one (see project_write_root).
        write_root = project_write_root(args.settings)
        require_inside(write_root, args.settings)
        require_inside(write_root, dest)
        require_inside(write_root, dest, follow=False)   # where --uninstall removes
        data = load_settings(args.settings)
        mtime_ns = os.stat(os.path.realpath(str(args.settings))).st_mtime_ns
        if args.uninstall:
            changes = uninstall(data, dest)
            # Only remove the hook file if we actually removed registrations for
            # it -- otherwise a run against an unrelated checkout would delete a
            # file that other, still-present registrations point at.
            if changes and dest.exists():
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
    except Exception as exc:                                    # noqa: BLE001
        # The project promises "nothing was changed" on a file it cannot handle
        # (e.g. a malformed but JSON-valid handler). Convert an unexpected error
        # into that contract instead of a traceback: nothing has been written yet.
        print("Could not process %s (%s: %s). Nothing was changed."
              % (args.settings, type(exc).__name__, exc), file=sys.stderr)
        return 1

    for foreign in foreign_same_basename_paths(data, dest):
        print("Note: a hook referencing %s at a different path was left untouched: %s\n"
              "      (it is not our installed copy -- remove it by hand if unexpected)."
              % (HOOK_NAME, foreign), file=sys.stderr)

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
    try:
        if args.uninstall:
            settings_changes = [c for c in changes if not c.startswith("remove hook file")]
            if settings_changes:
                backup = write_atomically(args.settings, data, mtime_ns, write_root)
            if changes and dest.exists():
                try:
                    os.remove(str(dest))
                except OSError as exc:
                    print("Warning: could not remove %s (%s) -- remove it by hand."
                          % (dest, exc.__class__.__name__), file=sys.stderr)
        else:
            # Copy the hook first: the registration must never point at a missing file.
            if copy_change:
                copy_hook(dest, write_root)
            settings_changes = [c for c in changes if c is not copy_change]
            if settings_changes:
                backup = write_atomically(args.settings, data, mtime_ns, write_root)
    except Abort as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("%sed rc-env-guard in %s:" % (verb.capitalize(), args.settings))
    for change in changes:
        print("  - %s" % change)
    if backup:
        print("\nBackup: %s" % backup)
    print("Restart Claude Code for the change to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
