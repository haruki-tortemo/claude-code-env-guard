#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Haruki Yoshino
"""Installer contract tests, with fault injection.

The installer edits a file the user cares about more than anything else we ship.
The cases that matter are the broken ones: a settings file we cannot parse must
leave the disk untouched, not get "repaired" into a fresh file.

Every case runs against a fixture in a temp dir, always at a path the guard
actually watches (`.../.claude/settings.json`). The real ~/.claude/settings.json
is never passed to the installer here.

Beyond the merge contract, this suite pins the four failure modes found in
review of the first published candidate:

  1. a checkout path with spaces produced an unquoted, broken command;
  2. a half-broken existing registration (narrowed matcher, `async: true`)
     was accepted as "already installed";
  3. `--settings` accepted paths the hook never inspects, so the install
     "succeeded" while guarding nothing;
  4. two backups in the same second collided and overwrote each other.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install" / "claude_code.py"
HOOK_SOURCE = ROOT / "hooks" / "rc_env_guard.py"
IGNORE = shutil.ignore_patterns(".git", "__pycache__")

EXISTING = {
    "model": "opus",
    "env": {"SOME_USER_VAR": "1"},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/other-guard"}]}
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "notify"}]}],
    },
}


def run(settings: pathlib.Path, *extra: str, installer: pathlib.Path = INSTALLER,
        home: pathlib.Path = None):
    """`home` sets HOME for the installer, so ~/.claude is a temp dir."""
    env = None if home is None else {**os.environ, "HOME": str(home)}
    proc = subprocess.run(
        [sys.executable, str(installer), "--settings", str(settings), *extra],
        capture_output=True, text=True, env=env,
    )
    return proc.returncode, proc.stdout + proc.stderr


def fixture(tmp: pathlib.Path, content, name="case") -> pathlib.Path:
    """Create <tmp>/<name>/.claude/settings.json - a path the guard watches."""
    d = tmp / name / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "settings.json"
    if content is not None:
        path.write_text(content if isinstance(content, str)
                        else json.dumps(content, indent=2), encoding="utf-8")
    return path


def dest_for(settings: pathlib.Path) -> pathlib.Path:
    return settings.parent / "hooks" / "rc_env_guard.py"


def backups(settings: pathlib.Path):
    return sorted(settings.parent.glob(settings.name + ".backup-*"))


def registered_commands(data: dict):
    return [h.get("command")
            for evs in (data.get("hooks") or {}).values() if isinstance(evs, list)
            for e in evs if isinstance(e, dict)
            for h in e.get("hooks", []) if isinstance(h, dict)]


def guard_commands(data: dict):
    return [c for c in registered_commands(data)
            if isinstance(c, str) and "rc_env_guard" in c]


def launch(command: str) -> int:
    """Run a registered command string the way a shell would, with an empty
    hook payload. Exit 1 (visible failure, undecidable payload) proves the
    process launched and reached the guard's own logic."""
    proc = subprocess.run(["bash", "-c", "echo '{}' | %s" % command],
                          capture_output=True, text=True)
    return proc.returncode


def load_module(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


results = []


def check(label: str, ok: bool, detail: str = ""):
    results.append((label, ok))
    print(("PASS " if ok else "FAIL ") + label + (("  -- " + detail) if detail and not ok else ""))


with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)

    # --- refuses targets the guard does not watch ---------------------------
    outside = tmp / "outside" / "custom.json"
    outside.parent.mkdir(parents=True)
    outside.write_text("{}", encoding="utf-8")
    code, out = run(outside)
    check("non-watched path: refused with explanation",
          code == 1 and "Refusing" in out, out[:80])
    check("non-watched path: file untouched", outside.read_text() == "{}")

    # --- refuses to touch what it cannot understand -------------------------
    missing = fixture(tmp, None, name="missing")
    code, out = run(missing)
    check("missing file: aborts, does not create", code == 1 and not missing.exists(), out[:80])

    broken = fixture(tmp, '{"hooks": {, }', name="broken")
    before = broken.read_text()
    code, out = run(broken)
    check("invalid JSON: aborts with line/column", code == 1 and "line" in out, out[:80])
    check("invalid JSON: file byte-identical afterwards", broken.read_text() == before)
    check("invalid JSON: no backup left behind", not backups(broken))

    notobj = fixture(tmp, '["a", "b"]', name="array")
    code, out = run(notobj)
    check("top-level array: aborts", code == 1 and notobj.read_text() == '["a", "b"]')

    badhooks = fixture(tmp, '{"hooks": "not-an-object"}', name="badhooks")
    code, out = run(badhooks)
    check("hooks is not an object: aborts", code == 1 and "not an object" in out)

    # --- dry run ------------------------------------------------------------
    dry = fixture(tmp, EXISTING, name="dry")
    before = dry.read_text()
    code, out = run(dry, "--dry-run")
    check("dry-run: reports 3 events", code == 0 and out.count("added ") == 3, out[:80])
    check("dry-run: reports the hook copy", "install hook file" in out, out[:120])
    check("dry-run: writes nothing",
          dry.read_text() == before and not dest_for(dry).exists())

    # --- the happy path preserves everything --------------------------------
    live = fixture(tmp, EXISTING, name="live")
    code, out = run(live)
    data = json.loads(live.read_text())
    dest = dest_for(live)
    check("install: exit 0", code == 0, out[:80])
    check("install: unrelated top-level keys survive",
          data.get("model") == "opus" and data["env"]["SOME_USER_VAR"] == "1")
    check("install: pre-existing PreToolUse hook survives",
          any(h.get("command") == "/usr/local/bin/other-guard"
              for e in data["hooks"]["PreToolUse"] for h in e.get("hooks", [])))
    check("install: pre-existing unrelated event survives", "Stop" in data["hooks"])
    check("install: all three events registered",
          all(ev in data["hooks"] for ev in ("PreToolUse", "SessionStart", "ConfigChange")))
    check("install: backup written", bool(backups(live)))
    check("install: hook copied next to settings",
          dest.exists() and dest.read_bytes() == HOOK_SOURCE.read_bytes())
    check("install: registration points at the copy, not the checkout",
          guard_commands(data) and all(str(ROOT) not in c for c in guard_commands(data)))
    check("install: registered command actually launches (exit 1 on empty payload)",
          launch(guard_commands(data)[0]) == 1)

    backup_count = len(backups(live))
    code, out = run(live)
    check("install twice: idempotent (no duplicate entry)",
          code == 0 and "Nothing to do" in out, out[:80])
    check("install twice: no second backup for a no-op",
          len(backups(live)) == backup_count)

    # --- installed settings satisfy the hook's own invariant ----------------
    hook_mod = load_module(dest, "rc_env_guard_installed_live")
    check("install: hook's registration_problem() accepts the result",
          hook_mod.registration_problem(data) is None,
          str(hook_mod.registration_problem(data)))

    # --- integration: a write to the installed settings is denied -----------
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Write",
               "tool_input": {"file_path": str(live),
                              "content": json.dumps({"env": {"DO_NOT_TRACK": "1"}})}}
    proc = subprocess.run([sys.executable, str(dest)], input=json.dumps(payload),
                          capture_output=True, text=True)
    check("integration: DO_NOT_TRACK write to installed settings -> exit 2",
          proc.returncode == 2, "exit=%d %s" % (proc.returncode, proc.stderr[:80]))

    # --- spaces in the path (found in review: unquoted command broke) -------
    spaced = fixture(tmp, EXISTING, name="dir with spaces")
    code, out = run(spaced)
    sdata = json.loads(spaced.read_text())
    check("space path: install exit 0", code == 0, out[:80])
    check("space path: registered command launches (exit 1 on empty payload)",
          bool(guard_commands(sdata)) and launch(guard_commands(sdata)[0]) == 1)

    # --- repair: half-broken registrations are not "already installed" ------
    def variant_settings(dest_path: pathlib.Path, **mut):
        cmd = mut.get("cmd", "python3 %s" % dest_path)
        handler = {"type": "command", "command": cmd}
        if mut.get("drop_type"):
            handler.pop("type")
        if mut.get("async_true"):
            handler["async"] = True
        entry = {"matcher": mut.get("matcher", "Edit|Write|MultiEdit|Bash"),
                 "hooks": [handler]}
        return {"hooks": {"PreToolUse": [entry]}}

    # These are genuinely OUR broken registration (they execute our dest): repair.
    forms = (
        ("async-true", dict(async_true=True)),
        ("matcher-shrink", dict(matcher="Bash")),
        ("type-missing", dict(drop_type=True)),
    )
    for label, mut in forms:
        path = fixture(tmp, None, name="repair-" + label)
        d = dest_for(path)
        mut = dict(mut)
        if "cmd" in mut:
            mut["cmd"] = mut["cmd"].format(d=d)
        path.write_text(json.dumps(variant_settings(d, **mut), indent=2), encoding="utf-8")
        code, out = run(path)
        after = json.loads(path.read_text())
        mod = load_module(d, "rc_env_guard_installed_" + label.replace("-", "_"))
        check("repair %s: exit 0 and repaired/added" % label,
              code == 0 and ("repaired PreToolUse" in out), out[:100])
        check("repair %s: result passes the hook's registration_problem()" % label,
              mod.registration_problem(after) is None,
              str(mod.registration_problem(after)))

    # Commands that do NOT execute our dest (our path is only an argument, or the
    # exit code is swallowed) are ambiguous, not ours: leave them, add a fresh
    # registration alongside. Deleting them would risk a third party's hook.
    preserve = (
        ("echo-arg", "echo {d}"),                 # our path is just an echo arg
        ("argv-decoy", "python3 -c 'pass' {d}"),  # -c script runs, {d} is argv[1]
        ("shell-or-true", "python3 {d} || true"),  # exit 2 swallowed; ambiguous form
    )
    for label, tmpl in preserve:
        path = fixture(tmp, None, name="preserve-" + label)
        d = dest_for(path)
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(HOOK_SOURCE.read_bytes())
        orig_cmd = tmpl.format(d=d)
        path.write_text(json.dumps(variant_settings(d, cmd=orig_cmd), indent=2),
                        encoding="utf-8")
        code, out = run(path)
        after = json.loads(path.read_text())
        cmds = [h.get("command") for e in after["hooks"]["PreToolUse"]
                for h in e.get("hooks", [])]
        mod = load_module(d, "rc_env_guard_installed_preserve_" + label.replace("-", "_"))
        check("preserve %s: our own registration added (not 'repaired')" % label,
              code == 0 and "added PreToolUse" in out, out[:120])
        check("preserve %s: the ambiguous handler is left untouched" % label,
              orig_cmd in cmds, str(cmds))
        check("preserve %s: result passes registration_problem()" % label,
              mod.registration_problem(after) is None)

    # A differently-named SYMLINK that resolves to our copy is a registration the
    # hook does not recognize (its basename is not rc_env_guard.py), so the
    # installer must not call it "already installed" - it adds a fresh, real
    # registration and the hook then agrees the guard is registered.
    aliased = fixture(tmp, None, name="alias-symlink")
    da = dest_for(aliased)
    da.parent.mkdir(parents=True, exist_ok=True)
    da.write_bytes(HOOK_SOURCE.read_bytes())
    alias = aliased.parent / "alias.py"
    os.symlink(da, alias)
    alias_cmd = "python3 %s" % alias
    matchers = {"PreToolUse": "Edit|Write|MultiEdit|Bash",
                "SessionStart": "startup|resume|clear|compact|fork",
                "ConfigChange": "user_settings|project_settings|local_settings|policy_settings"}
    aliased.write_text(json.dumps({"hooks": {
        ev: [{"matcher": m, "hooks": [{"type": "command", "command": alias_cmd}]}]
        for ev, m in matchers.items()}}), encoding="utf-8")
    code, out = run(aliased)
    aadata = json.loads(aliased.read_text())
    amod = load_module(da, "rc_env_guard_installed_alias")
    acmds = [h.get("command") for e in aadata["hooks"]["PreToolUse"]
             for h in e.get("hooks", [])]
    check("alias symlink: NOT reported as already installed (fresh added)",
          code == 0 and "Nothing to do" not in out and "added PreToolUse" in out, out[:120])
    check("alias symlink: the alias registration is left untouched",
          alias_cmd in acmds, str(acmds))
    check("alias symlink: installer and hook now agree (registration_problem None)",
          amod.registration_problem(aadata) is None,
          str(amod.registration_problem(aadata)))

    # And uninstall must not delete a command that merely mentions our path.
    echo_only = fixture(tmp, None, name="uninstall-echo-only")
    ed = dest_for(echo_only)
    echo_cmd = "echo %s" % ed
    echo_only.write_text(json.dumps(variant_settings(ed, cmd=echo_cmd), indent=2),
                         encoding="utf-8")
    code, out = run(echo_only, "--uninstall")
    eafter = json.loads(echo_only.read_text())
    ecmds = [h.get("command") for e in eafter["hooks"].get("PreToolUse", [])
             for h in e.get("hooks", [])]
    check("uninstall: a non-executing mention of our path is NOT removed",
          echo_cmd in ecmds, str(ecmds))

    # A same-basename hook at a DIFFERENT path is not ours: leave it, report it,
    # and add our own registration alongside (do not delete a third party's hook).
    decoy = fixture(tmp, None, name="repair-path-decoy")
    ddec = dest_for(decoy)
    decoy_cmd = "python3 /tmp/evil/rc_env_guard.py"
    decoy.write_text(json.dumps(variant_settings(ddec, cmd=decoy_cmd), indent=2),
                     encoding="utf-8")
    code, out = run(decoy)
    dafter = json.loads(decoy.read_text())
    dcmds = [h.get("command") for e in dafter["hooks"]["PreToolUse"]
             for h in e.get("hooks", [])]
    dmod = load_module(ddec, "rc_env_guard_installed_path_decoy")
    check("path-decoy: our own registration added (not 'repaired' by deleting the decoy)",
          code == 0 and "added PreToolUse" in out, out[:120])
    check("path-decoy: the different-path same-basename hook is left untouched",
          decoy_cmd in dcmds, str(dcmds))
    check("path-decoy: it is reported, not silently kept",
          "different path was left untouched" in out, out[:200])
    check("path-decoy: result still passes registration_problem()",
          dmod.registration_problem(dafter) is None)

    # --- uninstall removes ours (and the copy) and only ours -----------------
    code, out = run(live, "--uninstall")
    data = json.loads(live.read_text())
    check("uninstall: exit 0", code == 0, out[:80])
    check("uninstall: our entries gone", not guard_commands(data))
    check("uninstall: other guard untouched",
          any(h.get("command") == "/usr/local/bin/other-guard"
              for e in data["hooks"]["PreToolUse"] for h in e.get("hooks", [])))
    check("uninstall: unrelated event untouched", "Stop" in data["hooks"])
    check("uninstall: hook copy removed", not dest.exists())

    # --- checkout can move after install (found in review: stale entries) ---
    repo_a = tmp / "repoA"
    shutil.copytree(ROOT, repo_a, ignore=IGNORE)
    moved = fixture(tmp, EXISTING, name="moved")
    code, out = run(moved, installer=repo_a / "install" / "claude_code.py")
    check("move: install from checkout A", code == 0, out[:80])
    os.rename(repo_a, tmp / "repoB")  # the checkout moves away
    check("move: registered command still launches after the move",
          launch(guard_commands(json.loads(moved.read_text()))[0]) == 1)
    code, out = run(moved, "--uninstall")  # uninstall from a DIFFERENT checkout
    mdata = json.loads(moved.read_text())
    check("move: uninstall from another checkout removes all entries",
          code == 0 and not guard_commands(mdata), out[:100])
    check("move: hook copy removed", not dest_for(moved).exists())

    # --- a settings dir path with a shell metacharacter (found in review: a
    #     shlex.quote'd `&` broke command recognition, not just spaces) --------
    meta = fixture(tmp, EXISTING, name="R&D")
    code, out = run(meta)
    mdata = json.loads(meta.read_text())
    check("meta path: install exit 0", code == 0, out[:80])
    check("meta path: registered command launches (exit 1 on empty payload)",
          bool(guard_commands(mdata)) and launch(guard_commands(mdata)[0]) == 1)
    code, out = run(meta)   # recognizes its own registration -> idempotent
    check("meta path: second run is idempotent (recognizes its own registration)",
          code == 0 and "Nothing to do" in out, out[:100])
    mmod = load_module(dest_for(meta), "rc_env_guard_installed_meta")
    check("meta path: hook's registration_problem() accepts the result",
          mmod.registration_problem(json.loads(meta.read_text())) is None)

    # --- co-located third-party handler is preserved (handler granularity) ---
    colo = fixture(tmp, None, name="colocated")
    dcolo = dest_for(colo)
    dcolo.parent.mkdir(parents=True, exist_ok=True)
    dcolo.write_bytes(HOOK_SOURCE.read_bytes())
    other_cmd = "python3 /usr/local/bin/secret_scan.py"
    colo.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Edit|Write|MultiEdit|Bash", "hooks": [
            {"type": "command", "command": "python3 %s" % dcolo},
            {"type": "command", "command": other_cmd}]}]}}), encoding="utf-8")
    code, out = run(colo, "--uninstall")
    cdata = json.loads(colo.read_text())
    chandlers = [h.get("command") for e in cdata["hooks"]["PreToolUse"]
                 for h in e.get("hooks", [])]
    check("co-located: our handler removed", not guard_commands(cdata))
    check("co-located: the other handler in the same entry survives",
          other_cmd in chandlers, str(chandlers))

    # --- a co-located SAME-BASENAME hook at a DIFFERENT path must survive -----
    cob = fixture(tmp, None, name="colocated-basename")
    dcob = dest_for(cob)
    dcob.parent.mkdir(parents=True, exist_ok=True)
    dcob.write_bytes(HOOK_SOURCE.read_bytes())
    vendor_cmd = "python3 /opt/vendor/rc_env_guard.py"   # same basename, different path
    cob.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Edit|Write|MultiEdit|Bash", "hooks": [
            {"type": "command", "command": "python3 %s" % dcob},
            {"type": "command", "command": vendor_cmd}]}]}}), encoding="utf-8")
    code, out = run(cob, "--uninstall")
    cobd = json.loads(cob.read_text())
    cobh = [h.get("command") for e in cobd["hooks"]["PreToolUse"]
            for h in e.get("hooks", [])]
    check("same-basename: our copy removed", "python3 %s" % dcob not in cobh)
    check("same-basename: the different-path hook with our basename survives",
          vendor_cmd in cobh, str(cobh))

    # --- a malformed (non-dict) handler must not crash with a traceback ------
    nd = fixture(tmp, None, name="nondict")
    nd.write_text('{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [null]}]}}',
                  encoding="utf-8")
    code, out = run(nd)
    check("non-dict handler: no Python traceback",
          "Traceback" not in out, out[:120])
    check("non-dict handler: exit 0, three events registered",
          code == 0 and all(ev in json.loads(nd.read_text()).get("hooks", {})
                            for ev in ("PreToolUse", "SessionStart", "ConfigChange")),
          out[:120])

    # --- a symlinked ~/.claude/settings.json (dotfiles) is updated in place,
    #     not replaced. HOME is a temp dir, so this is the user's own ~/.claude;
    #     the same link in a project's .claude/ is refused (CWE-59, below). ----
    realdir = tmp / "realsettings" / ".claude"
    realdir.mkdir(parents=True)
    realfile = realdir / "settings.json"
    realfile.write_text(json.dumps(EXISTING), encoding="utf-8")
    linkdir = tmp / "linked" / ".claude"
    linkdir.mkdir(parents=True)
    linkpath = linkdir / "settings.json"
    os.symlink(realfile, linkpath)
    code, out = run(linkpath, home=tmp / "linked")
    check("symlink settings: install exit 0", code == 0, out[:80])
    check("symlink settings: path is still a symlink (not replaced by a file)",
          os.path.islink(linkpath))
    check("symlink settings: the real target received the registrations",
          all(ev in json.loads(realfile.read_text()).get("hooks", {})
              for ev in ("PreToolUse", "SessionStart", "ConfigChange")))

    # --- CWE-59: a project's .claude/ is repository content, and git checks
    #     committed symlinks out as symlinks. No link the repository may have
    #     committed (one inside a git work tree) may move a write or a delete
    #     out of the directory the user named - least of all into ~/.claude.
    #     A "clone" here is a directory with a `.git` entry. HOME is a temp dir
    #     throughout, so the real ~/.claude is never involved. ---------------
    home = tmp / "home"
    user_dir = home / ".claude"
    user_hooks = user_dir / "hooks"
    user_hooks.mkdir(parents=True)
    user_settings = user_dir / "settings.json"
    user_settings.write_text(json.dumps(EXISTING), encoding="utf-8")
    victim = home / "victim.txt"
    victim.write_text("KEEP\n", encoding="utf-8")

    def user_side_untouched() -> bool:
        return (json.loads(user_settings.read_text()) == EXISTING
                and not backups(user_settings)
                and not (user_hooks / "rc_env_guard.py").exists()
                and victim.read_text() == "KEEP\n")

    def clone_dir(name: str) -> pathlib.Path:
        d = tmp / name
        (d / ".git").mkdir(parents=True)
        return d

    def repo_fixture(content, name: str) -> pathlib.Path:
        clone_dir(name)
        return fixture(tmp, content, name=name)

    # Route 1: the hook file is a symlink out of the project.
    p = repo_fixture({}, "cwe59-hook-file")
    dest_for(p).parent.mkdir()
    os.symlink(str(victim), str(dest_for(p)))
    code, out = run(p, "--dry-run", home=home)
    check("project hook-file symlink out: dry-run refuses",
          code == 1 and "Refusing" in out, out[:200])
    code, out = run(p, home=home)
    check("project hook-file symlink out: refused, link target and settings untouched",
          code == 1 and "Refusing" in out and user_side_untouched()
          and p.read_text() == "{}" and not backups(p) and os.path.islink(dest_for(p)),
          out[:200])
    p = repo_fixture({}, "cwe59-hook-dangling")
    dest_for(p).parent.mkdir()
    os.symlink(str(home / "created.py"), str(dest_for(p)))
    code, out = run(p, home=home)
    check("project dangling hook-file symlink out: refused, nothing created",
          code == 1 and not (home / "created.py").exists(), out[:200])

    # Route 2: hooks/ is a symlink to ~/.claude/hooks.
    p = repo_fixture({}, "cwe59-hooks-dir")
    os.symlink(str(user_hooks), str(p.parent / "hooks"))
    code, out = run(p, home=home)
    check("project hooks/ symlink to ~/.claude/hooks: refused, nothing written there",
          code == 1 and "Refusing" in out and user_side_untouched()
          and p.read_text() == "{}", out[:200])

    # Route 3: .claude itself is a symlink to ~/.claude.
    linked_claude = clone_dir("cwe59-claude-dir") / ".claude"
    os.symlink(str(user_dir), str(linked_claude))
    code, out = run(linked_claude / "settings.json", home=home)
    check("project .claude symlink to ~/.claude: refused, user settings untouched",
          code == 1 and "Refusing" in out and user_side_untouched(), out[:200])

    # Route 4: settings.json is a symlink to ~/.claude/settings.json, absolute or
    # relative (the relative form only needs the clone depth).
    for label, target in (("absolute", str(user_settings)),
                          ("relative", os.path.join("..", "..", "home", ".claude",
                                                    "settings.json"))):
        p = repo_fixture(None, "cwe59-settings-" + label)
        os.symlink(target, str(p))
        code, out = run(p, home=home)
        check("project settings.json symlink (%s) to ~/.claude/settings.json: "
              "refused, no registration in user settings" % label,
              os.path.realpath(str(p)) == os.path.realpath(str(user_settings))
              and code == 1 and "Refusing" in out and user_side_untouched()
              and not dest_for(p).exists(), out[:200])

    # Route 5: a directory above .claude, inside the repository, is a symlink -
    # to the home directory (the run must not count as the user's own
    # ~/.claude install), to another project, or reached by `..`.
    anc = clone_dir("cwe59-ancestor")
    os.symlink(str(home), str(anc / "pkg"))
    code, out = run(anc / "pkg" / ".claude" / "settings.json", home=home)
    check("repository ancestor symlink resolving to ~: refused, not treated as ~/.claude",
          code == 1 and "Refusing" in out and user_side_untouched(), out[:200])
    other = fixture(tmp, {}, name="cwe59-other-project")
    os.symlink(str(other.parent.parent), str(anc / "pkg2"))
    code, out = run(anc / "pkg2" / ".claude" / "settings.json", home=home)
    check("repository ancestor symlink to another project: refused, resolved path named",
          code == 1 and os.path.realpath(str(other)) in out and other.read_text() == "{}"
          and not backups(other) and not dest_for(other).exists(), out[:300])
    code, out = run(pathlib.Path(os.path.realpath(str(other))), home=home)
    check("the resolved path named directly: installs",
          code == 0 and dest_for(other).exists(), out[:200])
    (home / "sub").mkdir()
    os.symlink(str(home / "sub"), str(anc / "sub"))
    code, out = run(anc / "sub" / ".." / ".claude" / "settings.json", home=home)
    check("`..` after a repository symlink (lands in ~/.claude): refused",
          code == 1 and "Refusing" in out and user_side_untouched(), out[:200])
    # The same ancestor link entered with `cd`, then a relative --settings: the
    # physical cwd has no link in it, so the installer must check the shell's
    # $PWD. Without that the run counts as the user's own ~/.claude install.
    logical = anc / "pkg"
    proc = subprocess.run(
        [sys.executable, str(INSTALLER), "--settings", ".claude/settings.json"],
        capture_output=True, text=True, cwd=str(logical),
        env={**os.environ, "HOME": str(home), "PWD": str(logical)})
    out = proc.stdout + proc.stderr
    check("`cd` through a repository symlink, relative --settings: refused",
          proc.returncode == 1 and "Refusing" in out and user_side_untouched(), out[:300])
    # A relative --settings is registered as an absolute command: a relative
    # `python3 .claude/hooks/rc_env_guard.py` would run each project's own file.
    rel = fixture(tmp, {}, name="cwe59-relative")
    proc = subprocess.run(
        [sys.executable, str(INSTALLER), "--settings", ".claude/settings.json"],
        capture_output=True, text=True, cwd=str(rel.parent.parent),
        env={**os.environ, "HOME": str(home), "PWD": str(rel.parent.parent)})
    cmds = registered_commands(json.loads(rel.read_text())) if proc.returncode == 0 else []
    check("relative --settings: installs with an absolute registered command",
          proc.returncode == 0 and cmds
          and all(os.path.isabs(shlex.split(c)[-1]) for c in cmds),
          (proc.stdout + proc.stderr)[:200] + " " + repr(cmds))
    other_clone = repo_fixture({}, "cwe59-other-clone")
    os.symlink(str(other_clone.parent.parent), str(anc / "pkg3"))
    code, out = run(anc / "pkg3" / ".claude" / "settings.json", home=home)
    check("repository ancestor symlink to another clone: refused, that clone untouched",
          code == 1 and "git work tree" in out and other_clone.read_text() == "{}"
          and not backups(other_clone) and not dest_for(other_clone).exists(), out[:300])
    # ~/.claude reached through a link outside any work tree (an unpacked
    # archive, say) does not count as naming ~/.claude, even when ~/.claude's
    # real directory is a dotfiles repo: only the filesystem root's links do.
    home5 = tmp / "home5"
    home5.mkdir()
    dot5 = clone_dir("dotfiles5") / "claude"
    dot5.mkdir()
    (dot5 / "settings.json").write_text("{}", encoding="utf-8")
    os.symlink(str(dot5), str(home5 / ".claude"))
    (tmp / "unpacked").mkdir()
    os.symlink(str(home5), str(tmp / "unpacked" / "pkg"))
    code, out = run(tmp / "unpacked" / "pkg" / ".claude" / "settings.json", home=home5)
    check("~/.claude reached through a link outside any work tree: refused, untouched",
          code == 1 and "Refusing" in out and (dot5 / "settings.json").read_text() == "{}"
          and not (dot5 / "hooks").exists(), out[:300])

    # Uninstall: its delete and its settings write must not follow those links
    # either. The user has a global install; a project points at it.
    code, out = run(user_settings, home=home)
    check("~/.claude install (HOME in a temp dir): exit 0",
          code == 0 and (user_hooks / "rc_env_guard.py").exists(), out[:200])
    snapshot = user_settings.read_bytes()
    global_cmd = guard_commands(json.loads(snapshot))[0]
    p = repo_fixture({"hooks": {"PreToolUse": [
        {"matcher": "Edit|Write|MultiEdit|Bash",
         "hooks": [{"type": "command", "command": global_cmd}]}]}},
        "cwe59-uninstall-hooks-dir")
    os.symlink(str(user_hooks), str(p.parent / "hooks"))
    code, out = run(p, "--uninstall", home=home)
    check("project hooks/ symlink to ~/.claude/hooks: uninstall refused, global copy kept",
          code == 1 and "Refusing" in out and (user_hooks / "rc_env_guard.py").exists()
          and user_settings.read_bytes() == snapshot, out[:200])
    linked_claude = clone_dir("cwe59-uninstall-claude-dir") / ".claude"
    os.symlink(str(user_dir), str(linked_claude))
    code, out = run(linked_claude / "settings.json", "--uninstall", home=home)
    check("project .claude symlink to ~/.claude: uninstall refused, global install kept",
          code == 1 and "Refusing" in out and (user_hooks / "rc_env_guard.py").exists()
          and user_settings.read_bytes() == snapshot, out[:200])
    # The delete removes the directory entry, not the file it resolves to: a
    # hooks/ link out whose entry links back inside must not get it removed.
    p = repo_fixture(None, "cwe59-uninstall-entry")
    back = p.parent / "vendored" / "rc_env_guard.py"
    back.parent.mkdir()
    back.write_bytes(HOOK_SOURCE.read_bytes())
    outside_hooks = tmp / "cwe59-outside-hooks"
    outside_hooks.mkdir()
    os.symlink(str(back), str(outside_hooks / "rc_env_guard.py"))
    os.symlink(str(outside_hooks), str(p.parent / "hooks"))
    p.write_text(json.dumps(variant_settings(dest_for(p))), encoding="utf-8")
    code, out = run(p, "--uninstall", home=home)
    check("project hooks/ symlink out, entry linking back in: uninstall refused, "
          "the outside entry kept",
          code == 1 and "Refusing" in out
          and os.path.islink(outside_hooks / "rc_env_guard.py"), out[:200])

    # Maintainer policy: a project's hook file is never written through a
    # symlink, even one that stays inside the settings directory - a committed
    # `hooks/rc_env_guard.py -> ../settings.local.json` would otherwise have the
    # hook copy overwrite the user's local settings. Install, dry-run and
    # uninstall all refuse, and neither the link nor its target changes.
    p = repo_fixture({}, "cwe59-hook-to-local")
    local = p.parent / "settings.local.json"
    local_bytes = json.dumps({"permissions": {"allow": ["Bash(ls)"]}}).encode()
    local.write_bytes(local_bytes)
    dest_for(p).parent.mkdir()
    os.symlink(os.path.join("..", "settings.local.json"), str(dest_for(p)))
    results_hook_link = [run(p, *extra, home=home) for extra in
                         ((), ("--dry-run",), ("--uninstall",))]
    check("project hook-file symlink to ../settings.local.json: install, dry-run and "
          "uninstall refused, local settings and the link untouched",
          all(c == 1 and "Refusing to write the hook through it" in o
              for c, o in results_hook_link)
          and local.read_bytes() == local_bytes and not backups(local)
          and os.path.islink(dest_for(p)) and p.read_text() == "{}" and not backups(p),
          str([o[:160] for _, o in results_hook_link]))
    # A link appearing at the hook file after that check (a race) is not
    # written through either: in a project, copy_hook replaces the entry.
    inst_race = load_module(INSTALLER, "installer_hook_race")
    p = repo_fixture({}, "cwe59-hook-race")
    local = p.parent / "settings.local.json"
    local.write_bytes(local_bytes)
    dest_for(p).parent.mkdir()
    os.symlink(os.path.join("..", "settings.local.json"), str(dest_for(p)))
    inst_race.copy_hook(dest_for(p), os.path.realpath(str(p.parent)))
    check("copy_hook in a project: a link at the hook file is replaced, not written through",
          local.read_bytes() == local_bytes and not os.path.islink(dest_for(p))
          and dest_for(p).read_bytes() == HOOK_SOURCE.read_bytes())
    p = repo_fixture({}, "cwe59-inner-hook")
    vendored = p.parent / "vendored" / "rc_env_guard.py"
    vendored.parent.mkdir()
    vendored.write_text("old\n", encoding="utf-8")
    dest_for(p).parent.mkdir()
    os.symlink(os.path.join("..", "vendored", "rc_env_guard.py"), str(dest_for(p)))
    code, out = run(p, home=home)
    check("project hook symlink inside .claude: refused, its target untouched",
          code == 1 and "Refusing to write the hook through it" in out
          and os.path.islink(dest_for(p)) and vendored.read_text() == "old\n"
          and p.read_text() == "{}", out[:200])
    # Kept: a settings-file or hooks/ link that stays inside a project's
    # settings directory is followed.
    p = repo_fixture({}, "cwe59-inner-hooks-dir")
    (p.parent / "vendor-hooks").mkdir()
    os.symlink("vendor-hooks", str(p.parent / "hooks"))
    code, out = run(p, home=home)
    check("project hooks/ symlink inside .claude: followed, hook written in its target",
          code == 0 and os.path.islink(p.parent / "hooks")
          and (p.parent / "vendor-hooks" / "rc_env_guard.py").read_bytes()
          == HOOK_SOURCE.read_bytes(), out[:200])
    p = repo_fixture(None, "cwe59-inner-settings")
    shared = p.parent / "shared.json"
    shared.write_text("{}", encoding="utf-8")
    os.symlink("shared.json", str(p))
    code, out = run(p, home=home)
    check("project settings.json symlink inside .claude: followed, updated in place",
          code == 0 and os.path.islink(p)
          and "PreToolUse" in json.loads(shared.read_text()).get("hooks", {}), out[:200])

    # Kept: in the user's own ~/.claude every link is followed (dotfiles) - a
    # hook-file link out of it, and ~/.claude itself as a directory link.
    home2 = tmp / "home2"
    dotfile = tmp / "dotfiles2" / "rc_env_guard.py"
    dotfile.parent.mkdir()
    dotfile.write_text("old\n", encoding="utf-8")
    h2 = fixture(tmp, {}, name="home2")
    dest_for(h2).parent.mkdir()
    os.symlink(str(dotfile), str(dest_for(h2)))
    code, out = run(h2, home=home2)
    check("~/.claude hook symlink to dotfiles: followed, updated in place",
          code == 0 and os.path.islink(dest_for(h2))
          and dotfile.read_bytes() == HOOK_SOURCE.read_bytes(), out[:200])
    home3 = tmp / "home3"
    home3.mkdir()
    dotdir = tmp / "dotfiles3" / "claude"
    dotdir.mkdir(parents=True)
    (dotdir / "settings.json").write_text("{}", encoding="utf-8")
    os.symlink(str(dotdir), str(home3 / ".claude"))
    code, out = run(home3 / ".claude" / "settings.json", home=home3)
    check("~/.claude as a dotfiles dir symlink: installed in the real dir",
          code == 0 and os.path.islink(home3 / ".claude")
          and (dotdir / "hooks" / "rc_env_guard.py").read_bytes() == HOOK_SOURCE.read_bytes()
          and "PreToolUse" in json.loads((dotdir / "settings.json").read_text())["hooks"],
          out[:200])

    # Maintainer policy (fail safe): a link outside any git work tree cannot be
    # told apart from one unpacked from an archive, so a project install refuses
    # it too - even the user's own link above a clone ($T/code -> $T/realcode).
    # The refusal names the resolved path, and passing that path installs.
    realcode = tmp / "realcode"
    clone_dir("realcode/proj")
    up = fixture(realcode, EXISTING, name="proj")
    os.symlink(str(realcode), str(tmp / "code"))
    named = tmp / "code" / "proj" / ".claude" / "settings.json"
    code, out = run(named, home=home)
    check("user's symlink above a clone: refused, resolved path named, clone untouched",
          code == 1 and "outside any git work tree" in out
          and os.path.realpath(str(up)) in out
          and json.loads(up.read_text()) == EXISTING and not backups(up)
          and not dest_for(up).exists(), out[:300])
    code, out = run(pathlib.Path(os.path.realpath(str(up))), home=home)
    updata = json.loads(up.read_text())
    check("user's symlink above a clone: the resolved path named instead installs",
          code == 0 and all(ev in updata["hooks"]
                            for ev in ("PreToolUse", "SessionStart", "ConfigChange"))
          and dest_for(up).read_bytes() == HOOK_SOURCE.read_bytes()
          and user_settings.read_bytes() == snapshot, out[:200])
    check("user's symlink above a clone: registered command launches",
          bool(guard_commands(updata)) and launch(guard_commands(updata)[0]) == 1)
    # Without a work tree around the target the same link is refused alike.
    nogit = fixture(tmp / "realcode-nogit", {}, name="proj")
    os.symlink(str(tmp / "realcode-nogit"), str(tmp / "code-nogit"))
    code, out = run(tmp / "code-nogit" / "proj" / ".claude" / "settings.json", home=home)
    check("user's symlink above a directory with no work tree: refused, resolved path named",
          code == 1 and "outside any git work tree" in out
          and os.path.realpath(str(nogit)) in out
          and nogit.read_text() == "{}" and not dest_for(nogit).exists(), out[:300])
    # Review route: an unpacked archive (no work tree) whose .claude links into
    # a sibling clone used to be followed because the TARGET lay in a work
    # tree, retargeting the install into that other project. Refused now, in
    # --dry-run and --uninstall too.
    sibling = repo_fixture(EXISTING, "cwe59-sibling-clone")
    archive = tmp / "cwe59-archive"
    archive.mkdir()
    os.symlink(os.path.join("..", "cwe59-sibling-clone", ".claude"), str(archive / ".claude"))
    archive_runs = [run(archive / ".claude" / "settings.json", *extra, home=home)
                    for extra in ((), ("--dry-run",), ("--uninstall",))]
    check("archive .claude -> ../<sibling clone>/.claude: refused, sibling untouched",
          all(c == 1 and "outside any git work tree" in o
              and os.path.realpath(str(sibling)) in o for c, o in archive_runs)
          and json.loads(sibling.read_text()) == EXISTING and not backups(sibling)
          and not dest_for(sibling).exists(), str([o[:200] for _, o in archive_runs]))
    os.symlink(os.path.join("..", "cwe59-sibling-clone"), str(archive / "pkg"))
    code, out = run(archive / "pkg" / ".claude" / "settings.json", home=home)
    check("archive pkg -> ../<sibling clone>: refused, sibling untouched",
          code == 1 and "outside any git work tree" in out
          and json.loads(sibling.read_text()) == EXISTING
          and not dest_for(sibling).exists(), out[:300])

    # Review objection 2: ~/.claude named another way ($HOME/x/../.claude) is
    # still the user's own install - its dotfiles settings link is followed.
    home4 = tmp / "home4"
    (home4 / "x").mkdir(parents=True)
    (home4 / ".claude").mkdir()
    dot4 = tmp / "dotfiles4" / "settings.json"
    dot4.parent.mkdir()
    dot4.write_text(json.dumps(EXISTING), encoding="utf-8")
    os.symlink(str(dot4), str(home4 / ".claude" / "settings.json"))
    named = home4 / "x" / ".." / ".claude" / "settings.json"
    code, out = run(named, home=home4)
    check("~/.claude named as $HOME/x/../.claude: installs through the dotfiles link",
          ".." in named.parts and code == 0
          and os.path.islink(home4 / ".claude" / "settings.json")
          and "PreToolUse" in json.loads(dot4.read_text())["hooks"]
          and (home4 / ".claude" / "hooks" / "rc_env_guard.py").exists(), out[:200])

    # Maintainer policy: "this is the user's ~/.claude" is decided by file
    # identity, not by spelling. A path that is ~/.claude's directory but not
    # named as ~/.claude is refused, with ~/.claude/settings.json to pass.
    # Simulated portably by a second path to the same directory: the user's
    # ~/.claude is a dotfiles link, and the dotfiles repo's real directory is
    # named directly (no link in the named path, so only identity can tell).
    home7 = tmp / "home7"
    home7.mkdir()
    dot7 = clone_dir("dotfiles7") / ".claude"
    dot7.mkdir()
    (dot7 / "settings.json").write_text(json.dumps(EXISTING), encoding="utf-8")
    os.symlink(str(dot7), str(home7 / ".claude"))
    code, out = run(dot7 / "settings.json", home=home7)
    check("~/.claude's real directory named directly: refused by identity, "
          "~/.claude/settings.json suggested, nothing changed",
          code == 1 and "named another way" in out
          and str(home7 / ".claude" / "settings.json") in out
          and json.loads((dot7 / "settings.json").read_text()) == EXISTING
          and not backups(dot7 / "settings.json") and not (dot7 / "hooks").exists(),
          out[:300])
    inst_id = load_module(INSTALLER, "installer_identity")
    linked7 = tmp / "home7-alias"
    os.symlink(str(home7), str(linked7))
    check("_same_dir: one directory under two spellings is the same; others are not",
          inst_id._same_dir(str(dot7), str(home7 / ".claude"))
          and inst_id._same_dir(str(linked7 / ".claude"), str(dot7))
          and not inst_id._same_dir(str(home7), str(dot7))
          and not inst_id._same_dir(str(tmp / "no-such-dir"), str(tmp / "no-such-dir")))
    # A spelling realpath does not canonicalize (as with letter case or the
    # firmlink), simulated on any host: os.stat maps a not-existing ALIAS8
    # onto home8, so only file identity can tell the two are one directory.
    home8 = tmp / "home8"
    (home8 / ".claude").mkdir(parents=True)
    (home8 / ".claude" / "settings.json").write_text(json.dumps(EXISTING), encoding="utf-8")
    alias_prefix = os.path.join(os.path.realpath(str(tmp)), "ALIAS8")
    real_prefix = os.path.realpath(str(home8))
    real_stat = os.stat

    def stat_alias(path, *a, **k):
        p = os.fspath(path)
        if p == alias_prefix or p.startswith(alias_prefix + os.sep):
            p = real_prefix + p[len(alias_prefix):]
        return real_stat(p, *a, **k)

    inst_id.DEFAULT_SETTINGS = home8 / ".claude" / "settings.json"
    os.stat = stat_alias
    try:
        try:
            verdict = inst_id.project_write_root(
                tmp / "ALIAS8" / ".claude" / "settings.json")
        except inst_id.Abort as exc:
            verdict = "refused: " + str(exc)
    finally:
        os.stat = real_stat
    check("~/.claude under a second spelling (simulated): refused by identity",
          str(verdict).startswith("refused: ") and "named another way" in str(verdict),
          str(verdict)[:200])
    # The real spellings, where this host has them: another letter case on a
    # case-insensitive volume, and macOS's /System/Volumes/Data firmlink. Each
    # check is added only where the second spelling truly reaches ~/.claude.
    spellings =[("another letter case", tmp / "HOME8" / ".claude" / "settings.json"),
                 ("the /System/Volumes/Data firmlink",
                  pathlib.Path("/System/Volumes/Data" + os.path.realpath(str(home8)))
                  / ".claude" / "settings.json")]
    for label, alias in spellings:
        if not inst_id._same_dir(str(alias.parent), str(home8 / ".claude")):
            print("SKIP ~/.claude named by %s: this host has no such second spelling" % label)
            continue
        code, out = run(alias, home=home8)
        check("~/.claude named by %s: refused by identity, nothing changed" % label,
              code == 1 and "named another way" in out
              and json.loads((home8 / ".claude" / "settings.json").read_text()) == EXISTING
              and not backups(home8 / ".claude" / "settings.json")
              and not (home8 / ".claude" / "hooks").exists(), out[:300])

    # Review objection 3: the decision does not trust a link because root owns
    # it - in a clone made by root every committed link is uid 0. Simulated
    # without root by making lstat report uid 0 for every entry.
    inst_r = load_module(INSTALLER, "installer_root_owned")
    inst_r.DEFAULT_SETTINGS = user_settings
    root_clone = clone_dir("cwe59-root-clone")
    os.symlink(str(user_dir), str(root_clone / ".claude"))
    os.symlink(str(home), str(root_clone / "pkg"))
    real_lstat, real_geteuid = os.lstat, getattr(os, "geteuid", None)

    def lstat_as_root(path, *a, **k):
        st = real_lstat(path, *a, **k)
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0,
                               st.st_size, int(st.st_atime), int(st.st_mtime),
                               int(st.st_ctime)))

    def decide(settings_path):
        try:
            return inst_r.project_write_root(settings_path)
        except inst_r.Abort:
            return "refused"

    os.lstat = lstat_as_root
    if real_geteuid is not None:
        os.geteuid = lambda: 1000
    try:
        simulated = os.lstat(str(root_clone / ".claude")).st_uid == 0
        verdicts = [decide(root_clone / ".claude" / "settings.json"),
                    decide(root_clone / "pkg" / ".claude" / "settings.json")]
        above = decide(tmp / "code" / "proj" / ".claude" / "settings.json")
    finally:
        os.lstat = real_lstat
        if real_geteuid is not None:
            os.geteuid = real_geteuid
    check("root-owned links in a clone (simulated uid 0): .claude and ancestor refused",
          simulated and verdicts == ["refused", "refused"], str(verdicts))
    check("root-owned link above a clone (simulated uid 0): refused, as any link",
          above == "refused", str(above))

    # Review objection 4: printed paths escape non-printable characters, so a
    # committed link target cannot rewrite or hide the refusal.
    check("_shown escapes control and bidi characters, keeps printable text",
          inst_r._shown("a\x1b[2Kb\x07\n\u202e/\u30d7\u30ed")
          == "a\\x1b[2Kb\\x07\\n\\u202e/\u30d7\u30ed")
    esc = clone_dir("cwe59-escape")
    os.symlink(str(home) + "/\x1b]0;owned\x07\x1b[2K\x1b[1A", str(esc / ".claude"))
    code, out = run(esc / ".claude" / "settings.json", home=home)
    check("escape: walk refusal shows the link target escaped, no raw ESC/BEL",
          code == 1 and "Nothing was changed." in out and "\x1b" not in out
          and "\x07" not in out and "\\x1b]0;owned\\x07" in out, repr(out[:300]))
    p = repo_fixture({}, "cwe59-escape-hook")
    dest_for(p).parent.mkdir()
    os.symlink(str(home) + "/\x1b[2K\x1b[1Ax.py", str(dest_for(p)))
    code, out = run(p, home=home)
    check("escape: hook-file link refusal shows the link target escaped",
          code == 1 and "Refusing" in out and "\x1b" not in out
          and "\\x1b[2K\\x1b[1Ax.py" in out, repr(out[:300]))
    p = repo_fixture({}, "cwe59-escape-hooks-dir")
    os.symlink(str(home) + "/\x1b[2K\x1b[1Ax", str(p.parent / "hooks"))
    code, out = run(p, home=home)
    check("escape: outside-the-directory refusal shows the resolved path escaped",
          code == 1 and "outside the settings directory" in out and "\x1b" not in out
          and "\\x1b[2K\\x1b[1Ax/rc_env_guard.py" in out, repr(out[:300]))

    # --- backups cannot collide within one second ---------------------------
    inst = load_module(INSTALLER, "installer_under_test")
    original = fixture(tmp, {"generation": 0}, name="backup")
    inst.time = types.SimpleNamespace(strftime=lambda fmt: "20260101T000000")
    b1 = inst.write_atomically(original, {"generation": 1})
    b2 = inst.write_atomically(original, {"generation": 2})
    check("backup collision: two distinct backup files", b1 != b2 and b1.exists() and b2.exists())
    check("backup collision: first backup keeps the original",
          json.loads(b1.read_text()).get("generation") == 0)
    check("backup collision: second backup keeps generation 1",
          json.loads(b2.read_text()).get("generation") == 1)
    check("backup: created 0600, not the source's mode (no world-readable secret copy)",
          (b1.stat().st_mode & 0o777) == 0o600, oct(b1.stat().st_mode & 0o777))

    # --- a concurrent change since we read the file is detected and aborted --
    conc = fixture(tmp, {"generation": 0}, name="concurrent")
    stale_mtime = os.stat(str(conc)).st_mtime_ns
    # Simulate another writer bumping the file after we captured its mtime.
    prev = inst.time
    inst.time = types.SimpleNamespace(strftime=lambda fmt: "20260101T111111")
    conc.write_text(json.dumps({"generation": 99}) + "\n", encoding="utf-8")
    aborted = False
    try:
        inst.write_atomically(conc, {"generation": 1}, stale_mtime)
    except inst.Abort:
        aborted = True
    inst.time = prev
    check("concurrent change: write_atomically aborts on a stale mtime", aborted)
    check("concurrent change: the concurrent writer's content is preserved",
          json.loads(conc.read_text()).get("generation") == 99)

    # --- fault injection: does the suite actually detect a destructive install?
    repo = tmp / "repo"
    shutil.copytree(ROOT, repo, ignore=IGNORE)
    mutant = repo / "install" / "claude_code.py"
    src = mutant.read_text()
    # Mutation: silently treat broken JSON as {} - the behavior that would
    # rebuild the user's settings from scratch.
    mutated = src.replace(
        "    except json.JSONDecodeError as exc:\n",
        "    except json.JSONDecodeError as exc:\n        return {}\n", 1)
    assert mutated != src, "mutation did not apply -- the test would be vacuous"
    mutant.write_text(mutated)
    broken2 = fixture(tmp, '{"hooks": {, }', name="broken2")
    proc = subprocess.run([sys.executable, str(mutant), "--settings", str(broken2)],
                          capture_output=True, text=True)
    clobbered = broken2.read_text() != '{"hooks": {, }'
    check("fault injection: a mutant that 'repairs' broken JSON is caught by our checks",
          clobbered or proc.returncode == 0,
          "mutant neither clobbered nor succeeded -- mutation may not have applied")

print()
failed = [label for label, ok in results if not ok]
if failed:
    print("FAILED: %d / %d" % (len(failed), len(results)))
    for label in failed:
        print("  - " + label)
    sys.exit(1)
print("%d checks passed" % len(results))
