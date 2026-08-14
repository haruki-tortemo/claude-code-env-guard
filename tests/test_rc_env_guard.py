#!/usr/bin/env python3
"""Regression suite for rc_env_guard.py. One line re-runs everything:

    python3 tests/test_rc_env_guard.py

House rule: whenever a pass count is reported, the path of the hook that
produced it is printed next to it. A pass count without its saved case table
is not evidence.

## The accident this guard prevents
A telemetry opt-out meant for an unrelated tool once landed `DO_NOT_TRACK=1`
in the env block of `~/.claude/settings.json`, and Remote Control (PC-to-phone
sync) died silently for three days. See README.md.

## Asymmetry
  - a miss (false negative)        = another three days lost
  - over-blocking (false positive) = trains circumvention
Hence negative cases (does it PASS what it must pass?) carry the same weight
as positive ones.

## The denominator is never a constant
Found in review: an earlier version hardcoded `total = 35`, so deleting a case
still printed "35/35" - the suite reproduced the very bug class it was written
to catch. This version derives the denominator from the set of executed IDs
and then reconciles it against an expected-ID manifest (EXPECTED_IDS):
anything missing, extra, or duplicated is a non-zero exit. The dynamic
denominator alone is not enough - after a deletion it would print "34/34" and
look fine - so the manifest reconciliation is part of the contract.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Defaults to the hook in this checkout - the installer registers the checkout
# copy, it does not install one elsewhere. Point RC_ENV_GUARD at another copy
# to test that one instead.
HOOK = Path(os.environ.get(
    "RC_ENV_GUARD",
    Path(__file__).resolve().parents[1] / "hooks" / "rc_env_guard.py"))
ALLOW, DENY, UNPROVABLE = 0, 2, 1

# Every case that must run (any missing one is a non-zero exit).
EXPECTED_IDS = (
    # Positive: 4 variables x write paths
    "deny-edit-donottrack", "deny-edit-growthbook", "deny-write-telemetry-zero",
    "deny-write-nonessential-zero", "deny-multiedit-second", "deny-write-unmeasured-value",
    # Positive: Bash shapes with settings as the write destination
    "deny-bash-mv", "deny-bash-cp", "deny-bash-redirect", "deny-bash-jq-then-mv",
    "deny-bash-rsync", "deny-bash-ln", "deny-bash-truncate", "deny-bash-sed-optorder",
    "deny-bash-dd",
    # Positive: line-continuation shapes that split segments (all four passed
    # live before the fix)
    "deny-bash-linecont-redirect", "deny-bash-linecont-tee",
    "deny-bash-linecont-cp", "deny-bash-linecont-sed",
    # Positive: mid-word folds (space replacement turns them into different
    # words and lets them pass)
    "deny-bash-linecont-midword", "deny-bash-linecont-midext", "deny-bash-linecont-midpath",
    # Positive: self-disarm, plus the forgery shapes found in review
    "deny-self-remove-both", "deny-self-command-swap", "deny-self-matcher-shrink",
    "deny-self-drop-configchange", "deny-self-command-comment-fake",
    "deny-self-matcher-substring-fake", "deny-self-broken-json",
    # Six shapes where the handler's effective conditions or the whole shell
    # line were not being checked
    "deny-self-async", "deny-self-if-filter", "deny-self-shell-or-true",
    "deny-self-argv-decoy", "deny-self-other-path-same-basename", "deny-self-type-missing",
    # Positive: through wrapper options
    "deny-bash-env-i-cp", "deny-bash-command-dashdash-cp", "deny-bash-sudo-n-cp",
    "deny-bash-env-u-cp",
    # Wrapper required-arguments and split-string
    "deny-bash-timeout-duration", "deny-bash-env-dash-s",
    # Negative
    "allow-edit-removal", "allow-edit-keep-existing", "allow-write-donottrack-zero",
    "allow-write-mention-outside-env", "allow-write-error-reporting", "allow-write-other-json",
    "allow-bash-commit-heredoc", "allow-bash-grep", "allow-bash-json-dumps",
    "allow-bash-stderr-redirect", "allow-bash-tee-elsewhere", "allow-bash-unrelated",
    "allow-bash-cp-backup", "allow-bash-mv-backup", "allow-self-reformat",
    # Negative: READS containing line continuations (the same hole's flip
    # side - plain reads were being blocked)
    "allow-bash-linecont-read", "allow-bash-linecont-loop",
    # Negative: metadata-only operations are outside the contract
    "allow-bash-touch", "allow-bash-chmod",
    "allow-bash-timeout-backup", "allow-bash-env-s-backup",
    # The undecidable is not silenced
    "err-broken-payload", "err-nonstring-content",
    # SessionStart
    "ss-user-clean", "ss-missing", "ss-user-hit", "ss-broken-json", "ss-bad-env-type",
    "ss-project-hit", "ss-local-hit", "ss-process-env", "ss-process-env-zero",
    "ss-warn-baseurl-spoof-suffix", "ss-warn-baseurl-spoof-userinfo",
    "ss-warn-baseurl-spoof-path", "ss-official-baseurl-silent", "ss-official-baseurl-slash",
    "ss-provider-bedrock", "ss-provider-vertex", "ss-provider-foundry",
    # ConfigChange (the after-the-fact backstop; reads the payload's file_path)
    "cc-block-dirty", "cc-clean-silent", "cc-policy-warn-only",
    "cc-filepath-project", "cc-filepath-local", "cc-filepath-policy-audit",
    "cc-policy-without-filepath",
    # Mutation detection
    "mut-sessionstart-noop", "mut-manifest-detects-deletion",
)

DIRTY_ENV = ("DO_NOT_TRACK", "DISABLE_TELEMETRY", "DISABLE_GROWTHBOOK",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_PROJECT_DIR",
             "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_UNIX_SOCKET",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
             "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD")


def run(payload: dict, env_extra: dict | None = None, hook: Path | None = None):
    # Drop every judgment-relevant variable before adding fixtures. The real
    # environment ships with ANTHROPIC_BASE_URL=https://api.anthropic.com by
    # default (measured) - it would leak into every case.
    env = {k: v for k, v in os.environ.items() if k not in DIRTY_ENV}
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(hook or HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )
    return proc.returncode, (proc.stdout + proc.stderr)


# --- scaffolding ------------------------------------------------------------

CLEAN = {"env": {"OTHER_TOOL_NO_TELEMETRY": "1"}}
DIRTY = {"env": {"DO_NOT_TRACK": "1"}}
GUARD_CMD = [{"type": "command", "command": "python3 %s" % HOOK}]
# The required registration spans all THREE events. If the fixture lacks one,
# pre_image_registers_guard() returns False and every self-disarm case passes
# as "a file that never registered the guard" - the suite would no longer be
# measuring the contract.
REGISTRATION = {
    "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|Bash", "hooks": GUARD_CMD}],
    "SessionStart": [{"matcher": "startup|resume|clear|compact|fork", "hooks": GUARD_CMD}],
    "ConfigChange": [{"matcher": "user_settings|project_settings|local_settings|policy_settings",
                      "hooks": GUARD_CMD}],
}


def write_settings(root: Path, body: dict, local: bool = False) -> str:
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    path = root / ".claude" / ("settings.local.json" if local else "settings.json")
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)


def edit(path: str, old: str, new: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Edit",
            "tool_input": {"file_path": path, "old_string": old, "new_string": new}}


def write(path: str, content) -> dict:
    if isinstance(content, dict):
        content = json.dumps(content, indent=2, ensure_ascii=False)
    return {"hook_event_name": "PreToolUse", "tool_name": "Write",
            "tool_input": {"file_path": path, "content": content}}


def bash(command: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command}}


def strip_registration(settings: dict, event: str | None = None) -> dict:
    # Match the fixture's own command exactly - a substring like "rc-env-guard"
    # would silently depend on the checkout's directory name.
    out = json.loads(json.dumps(settings))
    for ev in ([event] if event else list(out.get("hooks", {}))):
        out["hooks"][ev] = [e for e in out["hooks"].get(ev, [])
                            if not any(h.get("command") == GUARD_CMD[0]["command"]
                                       for h in e.get("hooks", []))]
    return out


def verify_manifest(executed: list[str]) -> list[str]:
    """Reconcile the executed-ID set against the expected manifest. Returns problems."""
    problems = []
    dupes = {i for i in executed if executed.count(i) > 1}
    if dupes:
        problems.append(f"duplicated IDs: {sorted(dupes)}")
    missing = [i for i in EXPECTED_IDS if i not in executed]
    if missing:
        problems.append(f"IDs that never ran: {missing}")
    extra = [i for i in executed if i not in EXPECTED_IDS]
    if extra:
        problems.append(f"IDs not in the manifest: {sorted(set(extra))}")
    return problems


def main() -> int:  # noqa: C901
    if not HOOK.exists():
        print(f"✗ hook not found: {HOOK}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="rc-guard-test-"))
    user_clean = write_settings(tmp / "clean", CLEAN)
    user_dirty = write_settings(tmp / "dirty", DIRTY)
    guarded_body = {"env": {"OTHER_TOOL_NO_TELEMETRY": "1"}, "hooks": REGISTRATION}
    guarded = write_settings(tmp / "guarded", guarded_body)

    executed: list[str] = []
    failed: list[str] = []

    def case(cid, name, expected, payload, env_extra=None, expect_text=None):
        executed.append(cid)
        code, out = run(payload, env_extra)
        ok = code == expected
        if ok and expect_text:
            ok = expect_text in out
        label = {ALLOW: "ALLOW", DENY: "DENY", UNPROVABLE: "VISIBLE-FAIL"}[expected]
        print(("✔" if ok else "✗") + f" [{label}] {name}"
              + ("" if ok else f"  → exit={code} out={out.strip()[:70]!r}"))
        if not ok:
            failed.append(cid)

    def ss(cid, name, env_extra, expect_in=None, expect_silent=False):
        executed.append(cid)
        code, out = run({"hook_event_name": "SessionStart"}, env_extra)
        ok = code == ALLOW and ((out.strip() == "") if expect_silent else (expect_in in out))
        print(("✔" if ok else "✗") + f" [SessionStart] {name}"
              + ("" if ok else f"  → exit={code} out={out.strip()[:80]!r}"))
        if not ok:
            failed.append(cid)

    print("--- Positive: 4 variables x write paths ---")
    case("deny-edit-donottrack", "Edit: adds DO_NOT_TRACK=1", DENY,
         edit(user_clean, '"OTHER_TOOL_NO_TELEMETRY": "1"',
              '"OTHER_TOOL_NO_TELEMETRY": "1",\n    "DO_NOT_TRACK": "1"'))
    case("deny-edit-growthbook", "Edit: adds DISABLE_GROWTHBOOK=1 (the 4th variable)", DENY,
         edit(user_clean, '"OTHER_TOOL_NO_TELEMETRY": "1"',
              '"OTHER_TOOL_NO_TELEMETRY": "1",\n    "DISABLE_GROWTHBOOK": "1"'))
    case("deny-write-telemetry-zero", 'Write: DISABLE_TELEMETRY="0" kills too', DENY,
         write(user_clean, {"env": {"DISABLE_TELEMETRY": "0"}}))
    case("deny-write-nonessential-zero", 'Write: NONESSENTIAL_TRAFFIC="0" kills too', DENY,
         write(user_clean, {"env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "0"}}))
    case("deny-multiedit-second", "MultiEdit: contamination arrives in the 2nd edit", DENY,
         {"hook_event_name": "PreToolUse", "tool_name": "MultiEdit",
          "tool_input": {"file_path": user_clean, "edits": [
              {"old_string": '"OTHER_TOOL_NO_TELEMETRY": "1"', "new_string": '"A": "1"'},
              {"old_string": '"A": "1"',
               "new_string": '"A": "1",\n    "DO_NOT_TRACK": "1"'}]}})
    case("deny-write-unmeasured-value", 'Write: unmeasured value "false" fails closed', DENY,
         write(user_clean, {"env": {"DO_NOT_TRACK": "false"}}))

    print("\n--- Positive: Bash with settings as the write destination ---")
    case("deny-bash-mv", "mv (temp -> settings)", DENY,
         bash("mv /tmp/rc-stage.json ~/.claude/settings.json"), expect_text="Edit or Write")
    case("deny-bash-cp", "cp wholesale replacement", DENY,
         bash("cp /tmp/rc-stage.json ~/.claude/settings.json"))
    case("deny-bash-redirect", "echo redirect", DENY,
         bash("echo '{\"env\":{\"DO_NOT_TRACK\":\"1\"}}' > ~/.claude/settings.json"))
    case("deny-bash-jq-then-mv", "jq -> mv (removal is steered to Edit/Write too)", DENY,
         bash("jq 'del(.env.DO_NOT_TRACK)' ~/.claude/settings.json > /tmp/s "
              "&& mv /tmp/s ~/.claude/settings.json"))
    case("deny-bash-rsync", "rsync (bypassed an earlier version)", DENY,
         bash("rsync /tmp/rc-stage.json ~/.claude/settings.json"))
    case("deny-bash-ln", "ln -sf (bypassed an earlier version)", DENY,
         bash("ln -sf /tmp/rc-stage.json ~/.claude/settings.json"))
    case("deny-bash-truncate", "truncate (could empty settings)", DENY,
         bash("truncate -s 0 ~/.claude/settings.json"))
    case("deny-bash-sed-optorder", "sed -E -i (option order used to slip through)", DENY,
         bash('sed -E -i "" "/DO_NOT_TRACK/d" ~/.claude/settings.json'))
    case("deny-bash-dd", "dd of=settings", DENY,
         bash("dd if=/tmp/rc-stage.json of=~/.claude/settings.json"))
    # Wrapper options used to be misread as the command head ("-i" etc.).
    case("deny-bash-env-i-cp", "env -i cp (through a wrapper option)", DENY,
         bash("env -i cp /tmp/rc-stage.json ~/.claude/settings.json"))
    case("deny-bash-command-dashdash-cp", "command -- cp", DENY,
         bash("command -- cp /tmp/rc-stage.json ~/.claude/settings.json"))
    case("deny-bash-sudo-n-cp", "sudo -n cp", DENY,
         bash("sudo -n cp /tmp/rc-stage.json ~/.claude/settings.json"))
    case("deny-bash-env-u-cp", "env -u FOO cp (a value-taking option)", DENY,
         bash("env -u FOO cp /tmp/rc-stage.json ~/.claude/settings.json"))
    # Wrapper required-arguments and split-string.
    case("deny-bash-timeout-duration", "timeout 5 cp (duration was misread as the command)", DENY,
         bash("timeout 5 cp /tmp/rc-stage.json ~/.claude/settings.json"))
    case("deny-bash-env-dash-s", "env -S 'cp ...' (split-string)", DENY,
         bash("env -S 'cp /tmp/rc-stage.json ~/.claude/settings.json'"))

    print("\n--- Positive: self-disarm is refused ---")
    case("deny-self-remove-both", "Write: removes the registrations wholesale", DENY,
         write(guarded, strip_registration(guarded_body)), expect_text="disarm")
    case("deny-self-command-swap", "Edit: swaps the command for `true`", DENY,
         edit(guarded, GUARD_CMD[0]["command"], "true"))
    shrunk = json.loads(json.dumps(guarded_body))
    shrunk["hooks"]["SessionStart"][0]["matcher"] = "startup"
    case("deny-self-matcher-shrink", "Write: shrinks the SessionStart matcher", DENY,
         write(guarded, shrunk))
    # The four forgeries that substring matching used to accept.
    no_cc = json.loads(json.dumps(guarded_body))
    del no_cc["hooks"]["ConfigChange"]
    case("deny-self-drop-configchange", "Write: drops only the ConfigChange registration", DENY,
         write(guarded, no_cc))
    fake_cmd = json.loads(json.dumps(guarded_body))
    for ev in ("PreToolUse", "SessionStart", "ConfigChange"):
        fake_cmd["hooks"][ev][0]["hooks"][0]["command"] = "true # rc_env_guard.py"
    case("deny-self-command-comment-fake", "Write: disguises the command as `true # rc_env_guard.py`",
         DENY, write(guarded, fake_cmd))
    fake_matcher = json.loads(json.dumps(guarded_body))
    fake_matcher["hooks"]["PreToolUse"][0]["matcher"] = "NotEdit|NotWrite|NotMultiEdit|NotBash"
    case("deny-self-matcher-substring-fake", "Write: disguises the matcher as NotEdit|... (anti-substring)",
         DENY, write(guarded, fake_matcher))
    case("deny-self-broken-json", "Write: wholesale replacement with broken JSON (registration unprovable)", DENY,
         write(guarded, "{"))
    # Six shapes where the filename is present but the fence does not work,
    # or the deny is swallowed.
    def variant(entry_extra=None, handler_extra=None, cmd=None, drop_type=False):
        b = json.loads(json.dumps(guarded_body))
        for ev in ("PreToolUse", "SessionStart", "ConfigChange"):
            entry = b["hooks"][ev][0]
            if entry_extra:
                entry.update(entry_extra)
            for h in entry["hooks"]:
                if handler_extra:
                    h.update(handler_extra)
                if cmd:
                    h["command"] = cmd
                if drop_type:
                    h.pop("type", None)
        return b

    case("deny-self-async", "Write: `async: true` (background = deny has no effect)", DENY,
         write(guarded, variant(handler_extra={"async": True})))
    case("deny-self-if-filter", "Write: narrows firing with `if` (fence no longer covers every event)", DENY,
         write(guarded, variant(entry_extra={"if": "Bash(git *)"})))
    case("deny-self-shell-or-true", "Write: swallows the exit 2 with `|| true`", DENY,
         write(guarded, variant(cmd=GUARD_CMD[0]["command"] + " || true")))
    case("deny-self-argv-decoy", "Write: argv decoy `python3 -c 'pass' ...`", DENY,
         write(guarded, variant(cmd="python3 -c 'pass' " + str(HOOK))))
    case("deny-self-other-path-same-basename", "Write: different path, same basename", DENY,
         write(guarded, variant(cmd="python3 /tmp/evil/rc_env_guard.py")))
    case("deny-self-type-missing", "Write: handler without `type`", DENY,
         write(guarded, variant(drop_type=True)))

    print("\n--- Negative (must pass) ---")
    case("allow-edit-removal", "Edit: a removal edit", ALLOW,
         edit(user_dirty, '"DO_NOT_TRACK": "1"', '"OTHER_TOOL_NO_TELEMETRY": "1"'))
    case("allow-edit-keep-existing", "Edit: touches another key, keeps existing contamination", ALLOW,
         edit(user_dirty, '"env": {', '"model": "opus",\n  "env": {'))
    case("allow-write-donottrack-zero", 'Write: DO_NOT_TRACK="0" does not kill', ALLOW,
         write(user_clean, {"env": {"DO_NOT_TRACK": "0"}}))
    case("allow-write-mention-outside-env", "Write: mention outside env (hooks.command)", ALLOW,
         write(user_clean, {"hooks": {"PreToolUse": [{"command": "echo DO_NOT_TRACK"}]},
                            "env": {}}))
    case("allow-write-error-reporting", "Write: DISABLE_ERROR_REPORTING is harmless", ALLOW,
         write(user_clean, {"env": {"DISABLE_ERROR_REPORTING": "1"}}))
    case("allow-write-other-json", "Write: not a settings file (mcp.json)", ALLOW,
         write(str(tmp / "clean" / ".claude" / "mcp.json"), {"env": {"DO_NOT_TRACK": "1"}}))
    # Line continuations (backslash + newline) used to split the segments and
    # break both directions. The four misses below: the destinations of `>`,
    # `tee`, `cp` and `sed -i` fell into a different segment, so "the side with
    # the write verb has no settings token / the side with settings has no
    # verb" and neither judgment fired - the stated contract was broken by one
    # backslash. Fix: run join_line_continuations() before segment splitting.
    case("deny-bash-linecont-redirect", "line continuation: folds right after >", DENY,
         bash('printf "{}" >\\\n   ~/.claude/settings.json'))
    case("deny-bash-linecont-tee", "line continuation: folds before tee's destination", DENY,
         bash('echo "{}" | tee \\\n   ~/.claude/settings.json'))
    case("deny-bash-linecont-cp", "line continuation: folds before cp's destination", DENY,
         bash('cp /tmp/rc-stage.json \\\n   ~/.claude/settings.json'))
    case("deny-bash-linecont-sed", "line continuation: folds before sed -i's target", DENY,
         bash('sed -i \\\n   -e s/a/b/ ~/.claude/settings.json'))
    # The fix for the four shapes above replaced the sequence with a SPACE and
    # created three fresh false negatives. Bash REMOVES an unquoted
    # backslash-newline from the input stream (GNU Bash Reference Manual);
    # replacing it with a space turns a mid-word fold into two words - the hook
    # read `sett ings.json` and passed it while the real shell wrote the file.
    # Hence removal, and these three cases to keep it that way.
    case("deny-bash-linecont-midword", "line continuation: mid-word (sett\\<nl>ings)", DENY,
         bash('printf "{}" > ~/.claude/sett\\\nings.json'))
    case("deny-bash-linecont-midext", "line continuation: right before the extension", DENY,
         bash('printf "{}" > ~/.claude/settings.\\\njson'))
    case("deny-bash-linecont-midpath", "line continuation: mid-path (.cla\\<nl>ude)", DENY,
         bash('printf "{}" > ~/.cla\\\nude/settings.json'))
    # The same hole's flip side: a stray trailing backslash made shlex raise
    # ValueError, and the except branch denied even plain READS.
    case("allow-bash-linecont-read", "line continuation: read only (grep + 2>/dev/null)", ALLOW,
         bash('grep -c DO_NOT_TRACK ~/.claude/settings.json \\\n   2>/dev/null'))
    case("allow-bash-linecont-loop", "line continuation: for-loop enumerating settings, read only", ALLOW,
         bash('for f in ~/.claude/settings.json \\\n'
              '         ~/.claude/settings.local.json; do\n'
              '  [ -e "$f" ] && grep -c env "$f" 2>/dev/null\n'
              'done'))
    case("allow-bash-commit-heredoc", "Bash: commit body mentions both tokens (heredoc)", ALLOW,
         bash("git add x.md && git commit -q -F - <<'EOF'\n"
              "infra: deny writes of DO_NOT_TRACK into `.claude/settings.json`\nEOF"))
    case("allow-bash-grep", "Bash: read-only grep", ALLOW,
         bash('grep -n "DO_NOT_TRACK" ~/.claude/settings.json'))
    case("allow-bash-json-dumps", "Bash: json.dumps display (no write)", ALLOW,
         bash('python3 -c "import json;d=json.load(open(\'~/.claude/settings.json\'));'
              'print(json.dumps(d[\'env\']))"'))
    case("allow-bash-stderr-redirect", "Bash: cat + 2>/dev/null", ALLOW,
         bash("cat ~/.claude/settings.json 2>/dev/null | head -5"))
    case("allow-bash-tee-elsewhere", "Bash: tee destination is not settings", ALLOW,
         bash("cat ~/.claude/settings.json | tee /tmp/backup.json"))
    case("allow-bash-unrelated", "Bash: unrelated to settings", ALLOW, bash("git status"))
    # Direction used to be ignored, so even backups were blocked.
    case("allow-bash-cp-backup", "Bash: cp settings -> /tmp (backup = source side)", ALLOW,
         bash("cp ~/.claude/settings.json /tmp/settings-backup.json"))
    case("allow-bash-mv-backup", "Bash: mv settings -> /tmp (removal direction)", ALLOW,
         bash("mv ~/.claude/settings.json /tmp/settings-backup.json"))
    case("allow-self-reformat", "Write: reformat, registrations kept", ALLOW,
         write(guarded, guarded_body))
    # Metadata-only operations used to be denied with a reason text ("use
    # Edit/Write instead") that was no alternative at all. Metadata is outside
    # the contract now.
    case("allow-bash-touch", "Bash: touch settings (content unchanged)", ALLOW,
         bash("touch ~/.claude/settings.json"))
    case("allow-bash-chmod", "Bash: chmod 600 settings (permissions only)", ALLOW,
         bash("chmod 600 ~/.claude/settings.json"))
    case("allow-bash-timeout-backup", "Bash: timeout 5 cp settings -> /tmp (backup)", ALLOW,
         bash("timeout 5 cp ~/.claude/settings.json /tmp/backup.json"))
    case("allow-bash-env-s-backup", "Bash: env -S backup shape", ALLOW,
         bash("env -S 'cp ~/.claude/settings.json /tmp/backup.json'"))

    print("\n--- The undecidable is not collapsed into exit 0 ---")
    case("err-broken-payload", "payload without tool_name", UNPROVABLE,
         {"hook_event_name": "PreToolUse"}, expect_text="could not evaluate")
    case("err-nonstring-content", "Write with non-string content", UNPROVABLE,
         {"hook_event_name": "PreToolUse", "tool_name": "Write",
          "tool_input": {"file_path": user_clean, "content": {"a": 1}}})

    print("\n--- SessionStart state distinctions ---")
    homes = {}
    for name, body in (("clean", CLEAN), ("dirty", DIRTY)):
        root = tmp / f"home-{name}"
        write_settings(root, body)
        homes[name] = str(root)
    for name, raw in (("broken", '{"env": {"DO_NOT_TRACK": "1",}}'),
                      ("badtype", '{"env": ["DO_NOT_TRACK"]}')):
        root = tmp / f"home-{name}"
        (root / ".claude").mkdir(parents=True)
        (root / ".claude" / "settings.json").write_text(raw)
        homes[name] = str(root)
    homes["missing"] = str(tmp / "home-missing")
    Path(homes["missing"]).mkdir()
    proj_dirty = tmp / "proj"
    write_settings(proj_dirty, DIRTY)
    proj_local = tmp / "projlocal"
    write_settings(proj_local, {"env": {"DISABLE_GROWTHBOOK": "1"}}, local=True)

    ss("ss-user-clean", "user clean = silent", {"HOME": homes["clean"]}, expect_silent=True)
    ss("ss-missing", "no settings file = silent", {"HOME": homes["missing"]}, expect_silent=True)
    ss("ss-user-hit", "contamination in user settings", {"HOME": homes["dirty"]},
       expect_in="user settings")
    ss("ss-broken-json", "broken JSON = cannot inspect", {"HOME": homes["broken"]},
       expect_in="cannot inspect")
    ss("ss-bad-env-type", "env is a list = cannot inspect", {"HOME": homes["badtype"]},
       expect_in="cannot inspect")
    ss("ss-project-hit", "contamination in project settings",
       {"HOME": homes["clean"], "CLAUDE_PROJECT_DIR": str(proj_dirty)},
       expect_in="project settings")
    ss("ss-local-hit", "contamination in local settings (4th variable)",
       {"HOME": homes["clean"], "CLAUDE_PROJECT_DIR": str(proj_local)},
       expect_in="local settings")
    ss("ss-process-env", "contamination in the process env = any arrival path",
       {"HOME": homes["clean"], "DO_NOT_TRACK": "1"}, expect_in="process environment")
    ss("ss-process-env-zero", 'process env DO_NOT_TRACK="0" = silent',
       {"HOME": homes["clean"], "DO_NOT_TRACK": "0"}, expect_silent=True)
    # Substring matching used to treat look-alike URLs as the official host.
    for cid, url in (("ss-warn-baseurl-spoof-suffix", "https://api.anthropic.com.evil.invalid"),
                     ("ss-warn-baseurl-spoof-userinfo", "https://api.anthropic.com@evil.invalid"),
                     ("ss-warn-baseurl-spoof-path", "https://evil.invalid/api.anthropic.com")):
        ss(cid, f"warn on look-alike base URL: {url}",
           {"HOME": homes["clean"], "ANTHROPIC_BASE_URL": url}, expect_in="Bedrock/Vertex")
    ss("ss-official-baseurl-silent", "official host = silent (present by default)",
       {"HOME": homes["clean"], "ANTHROPIC_BASE_URL": "https://api.anthropic.com"},
       expect_silent=True)
    ss("ss-official-baseurl-slash", "official host + trailing slash = silent",
       {"HOME": homes["clean"], "ANTHROPIC_BASE_URL": "https://api.anthropic.com/"},
       expect_silent=True)
    for cid, var in (("ss-provider-bedrock", "CLAUDE_CODE_USE_BEDROCK"),
                     ("ss-provider-vertex", "CLAUDE_CODE_USE_VERTEX"),
                     ("ss-provider-foundry", "CLAUDE_CODE_USE_FOUNDRY")):
        ss(cid, f"warn on official provider switch: {var}", {"HOME": homes["clean"], var: "1"},
           expect_in="Bedrock/Vertex")

    print("\n--- ConfigChange (after-the-fact backstop) ---")
    cc_dirty = tmp / "home-cc-dirty"
    write_settings(cc_dirty, {"env": {"DO_NOT_TRACK": "1"}, "hooks": REGISTRATION})
    cc_clean = tmp / "home-cc-clean"
    write_settings(cc_clean, guarded_body)
    case("cc-block-dirty", "a change leaving a killer env is blocked from this session", DENY,
         {"hook_event_name": "ConfigChange", "source": "user_settings"},
         {"HOME": str(cc_dirty)}, expect_text="Blocked")
    case("cc-clean-silent", "clean passes silently", ALLOW,
         {"hook_event_name": "ConfigChange", "source": "user_settings"},
         {"HOME": str(cc_clean)})
    case("cc-policy-warn-only", "policy_settings cannot be blocked = audit only", ALLOW,
         {"hook_event_name": "ConfigChange", "source": "policy_settings",
          "file_path": str(cc_dirty / ".claude" / "settings.json")},
         {"HOME": str(cc_clean)}, expect_text="audit only")
    # The payload's file_path used to be ignored in favour of reconstructed paths.
    for cid, source, body_env, note in (
            ("cc-filepath-project", "project_settings", {"DO_NOT_TRACK": "1"}, "project"),
            ("cc-filepath-local", "local_settings", {"DISABLE_GROWTHBOOK": "1"}, "local")):
        target = tmp / f"cc-{note}"
        path = write_settings(target, {"env": body_env}, local=(note == "local"))
        case(cid, f"{source}: killer env only on the file_path side ({note})", DENY,
             {"hook_event_name": "ConfigChange", "source": source, "file_path": path},
             {"HOME": str(cc_clean)}, expect_text="Blocked")
    policy_path = write_settings(tmp / "cc-policy", {"env": {"DISABLE_TELEMETRY": "1"}})
    case("cc-filepath-policy-audit", "policy_settings: actually reads file_path and audits", ALLOW,
         {"hook_event_name": "ConfigChange", "source": "policy_settings",
          "file_path": policy_path}, {"HOME": str(cc_clean)}, expect_text="DISABLE_TELEMETRY")
    case("cc-policy-without-filepath", "policy_settings without file_path = says so", ALLOW,
         {"hook_event_name": "ConfigChange", "source": "policy_settings"},
         {"HOME": str(cc_clean)}, expect_text="not been inspected")

    print("\n--- Do mutations go red? (proof the suite measures the contract) ---")
    executed.append("mut-sessionstart-noop")
    mutant = tmp / "mutant-hook.py"
    src = HOOK.read_text(encoding="utf-8")
    mutant.write_text(re.sub(r"def check_session_start\(\) -> None:\n",
                             "def check_session_start() -> None:\n    return\n", src, count=1),
                      encoding="utf-8")
    code, out = run({"hook_event_name": "SessionStart"},
                    {"HOME": homes["clean"], "DO_NOT_TRACK": "1"}, hook=mutant)
    ok = out.strip() == ""
    print(("✔" if ok else "✗")
          + " no-op check_session_start misses contamination = mutation detectable")
    if not ok:
        failed.append("mut-sessionstart-noop")

    # Does the manifest reconciliation catch deleting one case?
    executed.append("mut-manifest-detects-deletion")
    detected = bool(verify_manifest([i for i in executed if i != "deny-bash-rsync"]))
    print(("✔" if detected else "✗")
          + " deleting one case makes the manifest check non-zero = denominator is not a constant")
    if not detected:
        failed.append("mut-manifest-detects-deletion")

    problems = verify_manifest(executed)
    for p in problems:
        print(f"✗ manifest reconciliation: {p}")

    total = len(executed)
    print(f"\n{total - len(failed)}/{total} passed"
          f" (reconciled against the {len(EXPECTED_IDS)}-entry manifest)"
          f"   hook under test: {HOOK}")
    print("To re-enumerate the killer set against a live CLI: set each candidate"
          " variable to '1'/'0'/empty and run `claude remote-control --help`"
          " (see the hook docstring).")
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if (failed or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
