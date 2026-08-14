#!/usr/bin/env python3
"""rc-env-guard: stop environment variables from silently killing Claude Code Remote Control.

Events: PreToolUse (Edit|Write|MultiEdit|Bash), SessionStart, ConfigChange.

## What it blocks
Writing a Remote-Control-killing environment variable into the `env` block of
`settings.json`. Remote Control depends on feature-flag evaluation; one variable
kills PC-to-phone sync with no error anywhere. `remoteControlAtStartup: true`
stays in the file, so the config *looks* correct while sync is dead.

## Two tiers (measured against Claude Code v2.1.226)
DENY = the four variables that stop feature-flag evaluation. No use for them in a
       Claude Code settings file outweighs losing Remote Control silently (a tool
       that needs a telemetry opt-out almost always has its own dedicated switch).
WARN = variables that reroute or re-authenticate the API connection. Remote Control
       dies the same way, but these have legitimate uses (Bedrock, Vertex, custom
       gateways), so the hook warns at SessionStart instead of denying. Denying a
       legitimate operation trains the operator - human or model - to route around
       the guard, which is worse than the warning.

Value semantics were measured, not assumed:
    DO_NOT_TRACK                              "1" kills / "0" safe / empty safe
    DISABLE_GROWTHBOOK                        "1" kills / "0" safe / empty safe
    DISABLE_TELEMETRY                         "1" kills / **"0" also kills** / empty safe
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC  "1" kills / **"0" also kills** / empty safe
Unmeasured values ("false", etc.) are treated as killing (fail-closed).

## The set is not a permanent fact
Found in review: an earlier version hardcoded "the three variables I knew about" -
the set was bounded by the author's knowledge, not by measurement. External
behaviour changes with releases. After updating Claude Code, re-enumerate: set each
candidate variable to "1", "0", and empty, run `claude remote-control --help`, and
let the CLI itself name the offending variable in its refusal message.

## Decision contract
- Scope: writes to `.claude/settings.json` / `settings.local.json` only.
- Edit/Write/MultiEdit are judged on the **resulting JSON's `env`** (semantic, not
  substring - a mention of a variable outside `env` must not deny). When the
  post-image cannot be built (missing file, broken JSON), fall back to conservative
  string matching and say so in the reason text.
- Bash is denied when settings is the **write destination** of a known command
  shape. Variable names are deliberately not consulted: splitting into two steps
  (`printf > /tmp/x`, then `mv`) separates the name from the destination, so only
  the destination is judged. Backups (settings as *source*) pass. Enumerating
  shapes can never be complete, so the guarantee is stated as "prevents known
  direct writes"; the rest is caught after the fact by ConfigChange.
- Self-disarm is refused. In a settings file that registers this guard, all three
  registrations (PreToolUse / SessionStart / ConfigChange) are checked
  **structurally**: the matcher as a token set, the command target resolved via
  shlex + realpath, and a post-image that no longer parses as JSON cannot prove
  the registration survives, so it is denied. Editing the hook's *code* is free -
  only deregistration is the invariant.
- Deny = exit 2 + stderr (documented hook contract: exit 2 discards stdout and
  returns stderr to the model).
- Undecidable input is never silently allowed: payload parse failures and internal
  errors exit 1 + stderr - the operation proceeds, but it surfaces as a hook error
  instead of a silent success.

## SessionStart
1. Process env first - catches the variable no matter how it arrived (settings
   file, shell profile, `--settings`, managed policy). The original three-day
   outage would have been caught on day one by this check alone.
2. File scan (user / project / local), distinguishing missing / clean / hit /
   unreadable / invalid-json / invalid-env-type. Unreadable or broken files are
   **reported**, never silently treated as clean.
3. Always exit 0 (never blocks startup).

## ConfigChange (after-the-fact backstop)
Official sources: user_settings / project_settings / local_settings /
policy_settings. If the payload carries `file_path`, inspect that file first and
fall back to known paths only when it is absent (found in review: an earlier
version inspected only reconstructed paths and never read a single byte of policy
settings while claiming to audit them). If a killing variable remains, block
applying the change to the current session. `policy_settings` cannot be blocked
per the official contract - audit only. The file itself is already written;
rollback is not promised.

## What it does NOT guarantee
Arbitrary code execution (`python3 script.py` writing the file), obfuscation,
Bash shapes outside the enumeration, and `--settings` / managed injection are not
stopped at write time - they are caught only after the fact (ConfigChange) or at
the next SessionStart. Metadata-only operations (`touch` / `chmod`) are outside
the contract: they do not change content.

Guarantee statement: README.md ("What this does NOT do").
Interpreter floor rationale: docs/why-the-version-check-blocks.md.
Tests: tests/test_rc_env_guard.py.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from urllib.parse import urlsplit

# --- interpreter floor (inlined on purpose) ---------------------------------
# A hook must be a single file: Claude Code launches it by path, and requiring a
# sibling module breaks both `curl one-file` installs and any test that copies
# the hook elsewhere. The rationale for failing closed here is in docs/why-the-
# version-check-blocks.md; the contract is tested in tests/test_compat.py.
MIN_PYTHON = (3, 9)
if sys.version_info[:2] < MIN_PYTHON:
    sys.stderr.write(
        "rc-env-guard: refusing to run on Python %d.%d.%d (needs %d.%d or newer).\n"
        "This is a hard stop: a guard that cannot run must not report success.\n"
        "Interpreter in use: %s\n"
        "Fix: point the hook at a newer interpreter in ~/.claude/settings.json,\n"
        "or put a newer python3 first on PATH. If you would rather run\n"
        "unprotected, remove the hook entry -- do so deliberately.\n"
        % (sys.version_info[0], sys.version_info[1], sys.version_info[2],
           MIN_PYTHON[0], MIN_PYTHON[1], sys.executable))
    raise SystemExit(2)


BLOCK_EXIT = 2
ERROR_EXIT = 1

# Value semantics: "bool" ("0"/empty are safe) / "nonempty" (any non-empty value kills).
DENY_VARS: dict[str, str] = {
    "DO_NOT_TRACK": "bool",
    "DISABLE_GROWTHBOOK": "bool",
    "DISABLE_TELEMETRY": "nonempty",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "nonempty",
}
# Remote Control dies with these too, but they have legitimate uses - warn only.
# Every name below was confirmed against a live CLI, which refuses with:
#   "Remote Control is only available when using Claude via api.anthropic.com"
# The *_ANTHROPIC_AWS / *_ANTHROPIC_GOOGLE_CLOUD names are internal aliases that
# produced the same refusal in live testing; they are kept alongside the
# documented provider switches.
WARN_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_UNIX_SOCKET",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
)
SAFE_VALUES = ("", "0")
OFFICIAL_HOST = "api.anthropic.com"

# Invariant: the guard's own registration must survive every settings edit.
# Found in review: with Bash fully denied, the recommended Edit/Write path could
# still deregister the guard (measured exit 0) - a guard that cannot keep its own
# registration protects nothing.
# Also found in review: checking command and matcher as *substrings* let four
# disguises through (dropping only the ConfigChange registration; a command of
# `true # rc_env_guard.py`; a matcher of `NotEdit|...` - it contains "Edit" but
# never fires for the Edit tool; replacing the file with broken JSON).
# Substring matching accepts forgeries. Hence: all three events required, the
# matcher compared as a token set, the command target resolved via shlex.
REQUIRED_REGISTRATION = (
    ("PreToolUse", ("Edit", "Write", "MultiEdit", "Bash")),
    ("SessionStart", ("startup", "resume", "clear", "compact", "fork")),
    ("ConfigChange", ("user_settings", "project_settings",
                      "local_settings", "policy_settings")),
)
GUARD_SCRIPT = "rc_env_guard.py"
# Exact names only - found in review: a prefix match accepts "python3evil".
INTERPRETERS = {"python", "python3", "python3.11", "python3.12", "python3.13"}
# A command containing shell control or expansion is not accepted as a valid
# registration ("... || true" swallowed the exit 2 in testing).
SHELL_CONTROL = re.compile(r"\|\||&&|[;&|]|\$\(|`|>>?|<|\n")

SETTINGS_RE = re.compile(r"(?:^|/)\.claude/settings(?:\.local)?\.json$")
SETTINGS_MENTION = re.compile(r"settings(?:\.local)?\.json")
HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_.-]*)\1")
# Line continuation = backslash immediately followed by newline. Folded BEFORE
# segment splitting - join_line_continuations explains why.
LINE_CONTINUATION = re.compile(r"\\\n")
# Only the shape "settings is the WRITE DESTINATION" is judged - direction matters.
# History of the previous version, found in review:
#   1. bare substring checks on ">" and "json.dump" blocked read-only commands
#      (json.dumps, 2>/dev/null);
#   2. treating "mv|cp with the word settings anywhere in the command" as a deny
#      also blocked backups (cp settings /tmp/backup) - settings as *source* must
#      pass;
#   3. the enumeration missed rsync / ln / truncate / sed -E -i (option order) -
#      all four reproduced as exit-0 bypasses.
# Hence: judge the destination only and widen the enumeration. No enumeration is
# ever complete, so the guarantee is stated as "prevents known direct writes" and
# ConfigChange catches the rest after the fact.
_S = r"settings(?:\.local)?\.json"
SETTINGS_TOKEN = re.compile(_S)
REDIRECT_INTO_SETTINGS = re.compile(rf">>?\s*['\"]?[^\s'\"|;&]*{_S}")
WRITE_API = re.compile(
    r"""(?x)
      (?:json\.dump|write_text|writeFileSync)\s*\(   # write APIs (the "(" excludes json.dumps)
    | open\s*\([^)]*['"]w['"]                        # open(..., "w")
    """
)
# Commands whose last positional argument is the destination.
DEST_LAST = {"mv", "cp", "rsync", "install", "ln", "scp"}
# Commands that rewrite or empty the CONTENT of a file argument.
# touch/chmod/chown were once listed here and later removed - found in review:
# they do not change content, and the deny text said "use Edit/Write instead",
# which can change neither mtime nor permissions, so the suggested alternative
# was no alternative at all. Metadata damage is outside this guard's contract.
ANY_ARG_WRITERS = {"tee", "truncate", "patch", "shred"}
# In-place editors (a write only when -i is present).
INPLACE = {"sed", "perl", "ruby", "gsed"}
# Wrappers are stripped, together with their value-taking options. Found in
# review: `env -i cp`, `command -- cp` and `sudo -n cp` all passed because the
# head token was misread as "-i" (or similar) instead of the real command.
SHELL_PREFIX = {"sudo", "command", "env", "nohup", "time", "nice", "doas", "setsid",
                "xargs", "stdbuf", "timeout"}
WRAPPER_VALUE_OPTS = {
    "env": {"-u", "--unset"},
    "sudo": {"-u", "-g", "-p", "-C", "-r", "-t", "-U", "--user", "--group"},
    "doas": {"-u", "-C"},
    "nice": {"-n"},
    "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
    "xargs": {"-n", "-I", "-P", "-d", "-L", "-s"},
}
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def kills(var: str, value) -> bool:
    """Does var=value kill Remote Control? Unmeasured values count as killing (fail-closed)."""
    mode = DENY_VARS.get(var)
    if mode is None:
        return False
    text = "" if value is None else str(value)
    if mode == "nonempty":
        return text != ""
    return text not in SAFE_VALUES


def deny(reason: str) -> None:
    print(reason, file=sys.stderr)
    sys.exit(BLOCK_EXIT)


def report_error(reason: str) -> None:
    """Undecidable. Does not block the operation, but never stays silent
    (exit 1 surfaces as a hook error)."""
    print(f"⚠️ rc-env-guard: could not evaluate this operation ({reason}). "
          "A human needs to confirm that no Remote-Control-killing env variable "
          "slipped in.", file=sys.stderr)
    sys.exit(ERROR_EXIT)


DENY_TEMPLATE = """⛔ rc-env-guard: {what}

Why this is blocked: Claude Code's Remote Control depends on feature-flag
evaluation, and any one of four env variables **silently kills PC-to-phone
sync**. `remoteControlAtStartup: true` stays in the file, so the config looks
correct while sync is dead. This guard exists because exactly that once cost
three days: a telemetry opt-out meant for an unrelated tool landed in
settings.json and Remote Control died without a single error message.

What to do instead:
   Silence another tool's telemetry -> use that tool's own opt-out switch, not
                                       a global variable in Claude Code settings
   Reduce Claude Code's own traffic -> `DISABLE_ERROR_REPORTING=1` (measured:
                                       Remote Control survives it)
   Genuinely give up Remote Control -> the human owner must edit settings.json
                                       by hand, outside this session. Verbal
                                       approval to the model does not lift this.

The four variables (measured against v2.1.226): DO_NOT_TRACK and
DISABLE_GROWTHBOOK are safe at "0"; DISABLE_TELEMETRY and
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC kill **even at "0"**.
Details: README.md ("What it blocks")"""

BASH_TEMPLATE = """⛔ rc-env-guard: blocked a Bash write to `{path}`.

**No variable name was checked. Bash writes into settings are refused as such.**
Why:
  1. Split the write in two (`printf ... > /tmp/x`, then `mv`) and the variable
     name no longer sits next to the destination - name-based checks pass it.
  2. Replacing the whole settings file can also erase this guard's own hook
     registration - a guard you can quietly remove protects nothing.

-> Make settings changes with **Edit / Write** (the resulting JSON's env block
   is evaluated semantically). Removing a dangerous variable also passes there.
Details: README.md ("How it decides")"""

WARN_TEMPLATE = """⚠️ rc-env-guard (SessionStart): {vars} present in {where}.
Remote Control dies silently with this: the phone app's device list stays empty
and its log shows "Failed to toggle remote control". To confirm which variable,
run `claude remote-control --help` - the CLI names it in its refusal.
{note}Details: README.md"""


# --- semantic env parsing ---------------------------------------------------

def env_of(text: str) -> tuple[dict | None, str | None]:
    """Extract the env dict from a JSON string. Returns (env, failure reason)."""
    try:
        data = json.loads(text)
    except Exception as exc:                                    # noqa: BLE001
        return None, f"not parseable as JSON ({type(exc).__name__})"
    if not isinstance(data, dict):
        return None, "top level is not an object"
    env = data.get("env", {})
    if env is None:
        return {}, None
    if not isinstance(env, dict):
        return None, "env is not an object"
    return env, None


def first_killer(env: dict) -> tuple[str, str] | None:
    for var in DENY_VARS:
        if var in env and kills(var, env[var]):
            return var, str(env[var])
    return None


def heuristic_killer(text) -> str | None:
    """Conservative fallback when no post-image can be built: is a killer name in the string?"""
    if not isinstance(text, str):
        return None
    for var in DENY_VARS:
        if var in text:
            return var
    return None


# --- Bash -------------------------------------------------------------------

def strip_wrappers(tokens: list[str]) -> list[str]:
    """Strip env assignments and wrappers (plus their options and required
    arguments) so the real command lands at the head.

    Found in review: stripping only the options misread `timeout`'s required
    duration as the command (`timeout 5 cp ... settings` passed) and read
    `env -S 'cp ... settings'`'s split-string as a single command name. Each
    wrapper needs its own grammar.
    """
    tokens = list(tokens)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if ENV_ASSIGN.match(token):
            i += 1
            continue
        name = token.split("/")[-1]
        if name not in SHELL_PREFIX:
            break
        value_opts = WRAPPER_VALUE_OPTS.get(name, set())
        i += 1
        split_string = None
        while i < len(tokens):
            opt = tokens[i]
            if opt == "--":
                i += 1
                break
            if not opt.startswith("-"):
                break
            # `env -S '<command line>'` / `env -S<command line>`: re-split and execute
            if name == "env" and (opt == "-S" or opt.startswith("-S")):
                if opt == "-S" and i + 1 < len(tokens):
                    split_string = tokens[i + 1]
                    i += 2
                else:
                    split_string = opt[2:]
                    i += 1
                break
            if opt in value_opts:
                i += 2
                continue
            i += 1
        if split_string is not None:
            try:
                tokens = shlex.split(split_string) + tokens[i:]
            except ValueError:
                return tokens[i:]
            i = 0
            continue
        # Wrappers taking a REQUIRED positional argument after the options
        # (`timeout <duration> cmd ...`).
        if name == "timeout" and i < len(tokens) and not tokens[i].startswith("-"):
            i += 1
    return tokens[i:]


def settings_is_destination(segment: str) -> bool:
    """Is settings the WRITE DESTINATION of this simple command?

    Reads and backups (settings on the source side) pass. Syntax we cannot judge
    returns False (pass) and is handled by narrowing the stated guarantee
    instead - over-blocking trains circumvention, so the asymmetry leans this way.
    """
    if REDIRECT_INTO_SETTINGS.search(segment):
        return True
    if WRITE_API.search(segment) and SETTINGS_TOKEN.search(segment):
        return True
    try:
        tokens = shlex.split(segment, comments=True)
    except ValueError:
        # Unclosed quote - cannot tokenize. If it touches settings at all,
        # deny conservatively.
        return bool(SETTINGS_TOKEN.search(segment))
    tokens = strip_wrappers(tokens)
    if not tokens:
        return False
    head = tokens[0].split("/")[-1]
    args = tokens[1:]
    flags = [a for a in args if a.startswith("-")]
    positional = [a for a in args if not a.startswith("-")]

    if head == "dd":
        return any(a.startswith("of=") and SETTINGS_TOKEN.search(a) for a in args)
    if head in DEST_LAST:
        return bool(positional) and bool(SETTINGS_TOKEN.search(positional[-1]))
    if head in ANY_ARG_WRITERS:
        return any(SETTINGS_TOKEN.search(a) for a in positional)
    if head in INPLACE:
        inplace = any(f == "-i" or f.startswith("-i") or "i" in f.lstrip("-")[:3]
                      for f in flags)
        return inplace and any(SETTINGS_TOKEN.search(a) for a in positional)
    return False


def command_runs_guard(command) -> bool:
    """Does this command plainly execute this guard? Checked down to the argv.

    Found in review: "the basename appears somewhere and the head looks like an
    interpreter" let four shapes through (`... || true` swallowing the exit 2;
    the argv decoy `python3 -c 'pass' .../rc_env_guard.py`; a different path
    with the same basename, `/tmp/evil/rc_env_guard.py`; the prefix match
    `python3evil`). Hence: reject any shell control or expansion; accept exactly
    1 or 2 tokens; match the interpreter name exactly; and require the target's
    realpath to equal this very file.
    """
    if not isinstance(command, str) or SHELL_CONTROL.search(command):
        return False
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return False
    if len(tokens) == 1:
        target = tokens[0]
    elif len(tokens) == 2:
        if tokens[0].split("/")[-1] not in INTERPRETERS:
            return False
        target = tokens[1]
    else:
        return False
    if target.split("/")[-1] != GUARD_SCRIPT:
        return False
    try:
        return (os.path.realpath(os.path.expanduser(target))
                == os.path.realpath(os.path.abspath(__file__)))
    except Exception:                                           # noqa: BLE001
        return False


def matcher_tokens(matcher) -> set[str]:
    """The official matcher is a `|`-separated set - compare tokens, never substrings."""
    return {t.strip() for t in re.split(r"[|,]", str(matcher or "")) if t.strip()}


def registration_problem(data) -> str | None:
    """Does the settings post-image keep THIS GUARD'S OWN registration? Structural check."""
    hooks = (data or {}).get("hooks") or {}
    if not isinstance(hooks, dict):
        return "`hooks` is not an object"
    for event, needed in REQUIRED_REGISTRATION:
        entries = hooks.get(event) or []
        entry = None
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict):
                continue
            # An `if` filter makes the handler fire only on a match - the fence
            # no longer covers every event (official hooks reference, handler
            # common fields).
            if "if" in e:
                return (f"the `{event}` registration would gain an `if` filter, "
                        f"firing only on a match - the fence no longer covers "
                        f"every event")
            for h in (e.get("hooks") or []):
                if not isinstance(h, dict):
                    continue
                # `type` is required - accepting its absence reads an
                # out-of-schema handler as healthy.
                if h.get("type") != "command":
                    continue
                # `async: true` runs in the background = cannot block (official).
                if h.get("async") is True:
                    return (f"the `{event}` registration would become "
                            f"`async: true` - background execution **cannot "
                            f"deny anything**")
                if command_runs_guard(h.get("command")):
                    entry = e
                    break
            if entry:
                break
        if entry is None:
            return (f"no `{event}` registration would plainly execute "
                    f"rc-env-guard anymore")
        tokens = matcher_tokens(entry.get("matcher"))
        missing = [n for n in needed if n not in tokens]
        if missing:
            return (f"the `{event}` matcher would drop {'/'.join(missing)} "
                    f"(tokens = {sorted(tokens)})")
    return None


def join_line_continuations(command: str) -> str:
    """Fold line continuations (backslash + newline) the way the shell does.

    **Without this, the guard breaks in both directions** (observed live, six
    cases):

    1. **Misses (severe).** Splitting on newline drops `>` and its destination
       into different segments: one side has no settings token, the other has no
       write verb, so neither judgment fires. Four shapes passed live:
       `printf ... >\\<newline>settings.json`, `tee \\<newline>settings.json`,
       `cp src \\<newline>settings.json`, `sed -i \\<newline>-e ... settings.json`.
       The stated contract - "Bash writes into settings are denied" - was broken
       by a single backslash.
    2. **Over-blocking.** A stray trailing backslash makes `shlex.split` raise
       `ValueError: No escaped character`, and the except branch in
       `settings_is_destination` denies anything that mentions settings - which
       blocked plain *reads* (`grep`, `stat`).

    A line continuation is legitimate shell syntax, not an unclosed quote.
    Fold first, judge after.

    **Do NOT replace the sequence with a space - REMOVE it.** Bash removes an
    unquoted backslash-newline from the input stream (GNU Bash Reference
    Manual); it does not substitute anything. Replacing it with a space turns a
    token folded mid-word into two words: `~/.claude/sett\\<newline>ings.json`
    would read as `sett ings.json` and pass, while the real shell happily writes
    the file. We shipped exactly that bug: the space-replacement fix for the
    misses above created three fresh false negatives the same day - and a
    sibling guard in the same codebase had already hit this hole and fixed it by
    removal. Check prior art before re-implementing "the obvious fix".

    One divergence remains: backslash-newline inside single quotes is also
    removed here, which a real shell would keep. That divergence only ever makes
    a string look *more* like a write destination - it can over-block but never
    under-block, which is the safe direction.
    """
    return LINE_CONTINUATION.sub("", command)


def strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies. A commit message that merely *mentions* settings.json
    must not deny - hit once in real use."""
    lines = command.split("\n")
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        m = HEREDOC_START.search(line)
        if m:
            delimiter = m.group(2)
            i += 1
            while i < len(lines) and lines[i].strip() != delimiter:
                i += 1
            if i < len(lines):
                i += 1
            continue
        i += 1
    return "\n".join(kept)


# --- PreToolUse -------------------------------------------------------------

def apply_edits(path: str, edits: list[dict]) -> tuple[str | None, str | None]:
    """Build the post-image of applying edits to the file. Returns (content, failure reason)."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        text = ""
    except Exception as exc:                                    # noqa: BLE001
        return None, f"cannot read the existing file ({type(exc).__name__})"
    for edit in edits:
        old = edit.get("old_string")
        new = edit.get("new_string")
        if not isinstance(new, str) or not isinstance(old, str):
            return None, "old_string / new_string is not a string"
        if old == "":
            text = new
            continue
        if old not in text:
            return None, "old_string not found in the existing file"
        text = text.replace(old, new, 1) if not edit.get("replace_all") else text.replace(old, new)
    return text, None


def pre_image_registers_guard(path: str) -> bool:
    """Does the file, before the edit, register this guard?

    The invariant applies only where the guard is registered - a project
    settings file with no registration is not subject to it.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:                                           # noqa: BLE001
        return False
    return registration_problem(data) is None


def pre_image_killers(path: str) -> set[str]:
    """Killers already present before the edit - separates "this edit adds it"
    from "it was already there".

    Blocking an unrelated settings edit because of pre-existing contamination
    trains circumvention (over-blocking). Pre-existing contamination is
    SessionStart's job (process env + file scan).
    """
    try:
        with open(path, encoding="utf-8") as fh:
            env, _ = env_of(fh.read())
    except Exception:                                           # noqa: BLE001
        return set()
    if env is None:
        return set()
    return {v for v in DENY_VARS if v in env and kills(v, env[v])}


SELF_DISARM_TEMPLATE = """⛔ rc-env-guard: blocked a change that would disarm this guard itself ({what}).

Why: found in review - even with Bash writes fully denied, the recommended
Edit/Write path could still deregister this guard (measured exit 0). From the
next session on, no Remote-Control-killing env variable would be checked and no
SessionStart warning would appear. A guard that can be quietly removed protects
nothing.

-> To remove it deliberately, the human owner must edit settings.json outside
   of Claude Code. Verbal approval to the model does not disarm this guard.
-> Fixing the hook's *code* is free: this invariant only checks that the
   registrations and matchers survive.
File: {path}"""


def judge_registration(path: str, content: str, source: str) -> None:
    """If this file registers the guard, does the registration survive the post-image?"""
    if not pre_image_registers_guard(path):
        return
    try:
        data = json.loads(content)
    except Exception as exc:                                    # noqa: BLE001
        # Found in review: wholesale replacement with broken JSON used to pass -
        # the next session could not have read the registration. "Never silently
        # allow the undecidable" applies to the self-registration check too.
        print(SELF_DISARM_TEMPLATE.format(
            what=f"the post-image does not parse as JSON, so the registration "
                 f"cannot be proven to survive ({type(exc).__name__} / {source})",
            path=path), file=sys.stderr)
        sys.exit(BLOCK_EXIT)
    problem = registration_problem(data)
    if problem:
        print(SELF_DISARM_TEMPLATE.format(what=f"{problem} / {source}", path=path),
              file=sys.stderr)
        sys.exit(BLOCK_EXIT)


def judge_post_image(path: str, content: str, source: str, fallback_text) -> None:
    judge_registration(path, content, source)
    already = pre_image_killers(path)
    env, why = env_of(content)
    if env is None:
        var = heuristic_killer(fallback_text)
        if var and var not in already:
            deny(DENY_TEMPLATE.format(
                what=f"`{var}` is about to enter `{path}` ({source}: {why}, so "
                     f"this was judged **conservatively by string matching**)"))
        return
    for var in DENY_VARS:
        if var in env and kills(var, env[var]) and var not in already:
            deny(DENY_TEMPLATE.format(
                what=f"the env block of `{path}` would gain `{var}={env[var]}` "
                     f"({source})"))


def check_pre_tool_use(payload: dict) -> None:
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        # The harness always sends tool_name - absence means undecidable.
        # Never collapse that into a silent exit 0.
        report_error("PreToolUse payload has no tool_name")
    ti = payload.get("tool_input") or {}

    if tool == "Bash":
        command = ti.get("command")
        if not isinstance(command, str):
            return
        # Order matters: heredoc terminators are found line-wise, so strip them
        # FIRST; fold line continuations BEFORE segment splitting.
        stripped = join_line_continuations(strip_heredoc_bodies(command))
        if not SETTINGS_MENTION.search(stripped):
            return
        for segment in re.split(r"\|\||&&|[;|&\n]", stripped):
            if segment.strip() and settings_is_destination(segment):
                deny(BASH_TEMPLATE.format(path=".claude/settings*.json"))
        return

    path = ti.get("file_path")
    if not isinstance(path, str) or not SETTINGS_RE.search(path):
        return

    if tool == "Write":
        content = ti.get("content")
        if not isinstance(content, str):
            report_error("Write content is not a string")
        judge_post_image(path, content, "the JSON that Write would produce", content)
        return

    if tool == "Edit":
        edits = [ti]
    elif tool == "MultiEdit":
        edits = ti.get("edits") or []
        if not isinstance(edits, list):
            report_error("MultiEdit edits is not a list")
    else:
        return

    post, why = apply_edits(path, edits)
    if post is None:
        var = heuristic_killer(" ".join(str(e.get("new_string", "")) for e in edits))
        old = " ".join(str(e.get("old_string", "")) for e in edits)
        if var and var not in old and var not in pre_image_killers(path):
            deny(DENY_TEMPLATE.format(
                what=f"`{var}` is about to enter `{path}` (no post-image could "
                     f"be built - **judged conservatively by string matching**: "
                     f"{why})"))
        return
    judge_post_image(path, post, f"the JSON after applying {tool}",
                     " ".join(str(e.get("new_string", "")) for e in edits))


# --- SessionStart -----------------------------------------------------------

def base_url_is_official(value: str) -> bool:
    """Does ANTHROPIC_BASE_URL really point at the official host?

    Never check this by substring - found in review:
    `https://api.anthropic.com.evil.invalid`, `https://api.anthropic.com@evil.invalid`
    and `https://evil.invalid/api.anthropic.com` were all treated as official and
    passed without a warning (measured). Judge by exact hostname match.
    """
    try:
        parts = urlsplit(value.strip())
    except Exception:                                           # noqa: BLE001
        return False
    return parts.scheme == "https" and (parts.hostname or "").lower() == OFFICIAL_HOST


def settings_paths() -> list[tuple[str, str]]:
    paths = [("user", os.path.expanduser("~/.claude/settings.json"))]
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        paths += [
            ("project", os.path.join(project, ".claude", "settings.json")),
            ("local", os.path.join(project, ".claude", "settings.local.json")),
        ]
    return paths


def check_session_start() -> None:
    # 1. Process env - catches the variable whether it arrived via a settings
    #    file, the shell, `--settings`, or managed policy.
    hits = [v for v in DENY_VARS if v in os.environ and kills(v, os.environ[v])]
    # ANTHROPIC_BASE_URL is present BY DEFAULT with the official host (measured:
    # desktop-app sessions ship with `https://api.anthropic.com` already set).
    # Warning without looking at the value would fire on every session - and a
    # warning that always fires is a warning nobody reads.
    warns = []
    for var in WARN_VARS:
        value = os.environ.get(var, "")
        if not value:
            continue
        if var == "ANTHROPIC_BASE_URL" and base_url_is_official(value):
            continue
        warns.append(var)
    if hits:
        print(WARN_TEMPLATE.format(
            where="**this session's process environment**", vars=" / ".join(hits),
            note="It arrived via a settings file, the shell, `--settings`, or "
                 "managed policy. After removing it, start a NEW session - "
                 "existing processes keep the old env.\n"))
    if warns:
        print(f"⚠️ rc-env-guard: {' / '.join(warns)} present in the process env "
              "- Remote Control will refuse to start, because the session is "
              "not talking to api.anthropic.com with subscription auth. If this "
              "is deliberate (Bedrock/Vertex or a custom gateway), ignore this "
              "line.")

    # 2. File scan - auxiliary: tells you where to remove the variable from.
    for scope, path in settings_paths():
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except Exception as exc:                                # noqa: BLE001
            print(f"⚠️ rc-env-guard: cannot read {scope} settings ({path} / "
                  f"{type(exc).__name__}) - **cannot inspect it**. Not treated "
                  f"as clean.")
            continue
        env, why = env_of(text)
        if env is None:
            print(f"⚠️ rc-env-guard: cannot inspect {scope} settings ({path} / "
                  f"{why}). The absence of Remote-Control-killing env variables "
                  f"is NOT confirmed.")
            continue
        found = [v for v in DENY_VARS if v in env and kills(v, env[v])]
        if found:
            print(WARN_TEMPLATE.format(
                where=f"{scope} settings ({path})", vars=" / ".join(found),
                note="Remove it, then start a NEW session.\n"))


CONFIG_CHANGE_TEMPLATE = """⛔ rc-env-guard (ConfigChange): {vars} found in {where}.
**Blocked this change from applying to the current session.**

This is the after-the-fact backstop for paths that slip past the PreToolUse
enumeration (`rsync`, `ln`, arbitrary scripts, ...).
⚠️ **The file itself is already written** - this block only guarantees that the
change does not apply to the current session. Remove the variable via Edit/Write
(or by the human owner's hand).
⚠️ `policy_settings` cannot be blocked per the official contract - audit only.
Details: README.md"""


def check_config_change(payload: dict) -> None:
    """Inspect the RESULT of a config change (source is read; payload details
    are not trusted further than that).

    Official sources: user_settings / project_settings / local_settings /
    policy_settings (confirmed against the binary's strings). policy_settings
    cannot be blocked, so it is audited only.
    A missing self-registration is NOT blocked here: ConfigChange also fires
    when the human owner edits the file by hand. Stopping the model's
    self-disarm is the PreToolUse invariant's job - that one never triggers on
    hand edits.
    """
    source = str(payload.get("source") or "")
    blocking = source != "policy_settings"

    # Found in review: an earlier version never read the payload's `file_path`
    # and looked only at paths reconstructed from HOME and CLAUDE_PROJECT_DIR.
    # It missed project/local settings whenever the project env was absent, and
    # never read a single byte of policy settings (which have no known path)
    # while claiming "audit only" - a claim wider than the implementation.
    file_path = payload.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        targets = [(source or "payload", file_path)]
    else:
        targets = settings_paths()
        if source == "policy_settings":
            print("⚠️ rc-env-guard (ConfigChange): source=policy_settings but no "
                  "`file_path` - **the policy settings have not been inspected** "
                  "(they have no known path). Do not call this audited.")

    for scope, path in targets:
        if not os.path.exists(path):
            print(f"⚠️ rc-env-guard (ConfigChange): target {path} for {scope} "
                  f"not found - cannot inspect it.")
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                env, why = env_of(fh.read())
        except Exception as exc:                                # noqa: BLE001
            print(f"⚠️ rc-env-guard (ConfigChange): cannot read {path} "
                  f"({type(exc).__name__}) - cannot inspect it. Not treated as "
                  f"clean.")
            continue
        if env is None:
            print(f"⚠️ rc-env-guard (ConfigChange): cannot inspect {path} ({why}).")
            continue
        found = [v for v in DENY_VARS if v in env and kills(v, env[v])]
        if found:
            message = CONFIG_CHANGE_TEMPLATE.format(
                where=f"{scope} settings ({path})", vars=" / ".join(found))
            if blocking:
                print(message, file=sys.stderr)
                sys.exit(BLOCK_EXIT)
            print(message + "\n(source=policy_settings, so blocking is "
                            "impossible - audit only)")

    if not pre_image_registers_guard(os.path.expanduser("~/.claude/settings.json")):
        print("⚠️ rc-env-guard (ConfigChange): **this guard's own registration "
              "is gone from user settings**. From the next session on, "
              "Remote-Control-killing env variables will not be checked. (If "
              "the human owner removed it deliberately, this is as intended.)")


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:                                    # noqa: BLE001
        report_error(f"cannot parse the hook input JSON ({type(exc).__name__})")
        return

    event = payload.get("hook_event_name")
    if event == "ConfigChange":
        try:
            check_config_change(payload)
        except SystemExit:
            raise
        except Exception as exc:                                # noqa: BLE001
            print(f"⚠️ rc-env-guard: the ConfigChange inspection failed "
                  f"({type(exc).__name__}) - nothing was verified.")
        sys.exit(0)

    if event == "SessionStart":
        try:
            check_session_start()
        except Exception as exc:                                # noqa: BLE001
            print(f"⚠️ rc-env-guard: the SessionStart inspection failed "
                  f"({type(exc).__name__}) - nothing was verified.")
        sys.exit(0)

    try:
        check_pre_tool_use(payload)
    except SystemExit:
        raise
    except Exception as exc:                                    # noqa: BLE001
        report_error(f"internal error ({type(exc).__name__}: {exc})")
    sys.exit(0)


if __name__ == "__main__":
    main()
