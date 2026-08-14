#!/usr/bin/env python3
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


def run(settings: pathlib.Path, *extra: str, installer: pathlib.Path = INSTALLER):
    proc = subprocess.run(
        [sys.executable, str(installer), "--settings", str(settings), *extra],
        capture_output=True, text=True,
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

    forms = (
        ("async-true", dict(async_true=True)),
        ("matcher-shrink", dict(matcher="Bash")),
        ("shell-or-true", dict(cmd="python3 {d} || true")),
        ("type-missing", dict(drop_type=True)),
        ("argv-decoy", dict(cmd="python3 -c 'pass' {d}")),
        ("path-decoy", dict(cmd="python3 /tmp/evil/rc_env_guard.py")),
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

    # --- a symlinked settings.json is updated in place, not replaced ---------
    realdir = tmp / "realsettings" / ".claude"
    realdir.mkdir(parents=True)
    realfile = realdir / "settings.json"
    realfile.write_text(json.dumps(EXISTING), encoding="utf-8")
    linkdir = tmp / "linked" / ".claude"
    linkdir.mkdir(parents=True)
    linkpath = linkdir / "settings.json"
    os.symlink(realfile, linkpath)
    code, out = run(linkpath)
    check("symlink settings: install exit 0", code == 0, out[:80])
    check("symlink settings: path is still a symlink (not replaced by a file)",
          os.path.islink(linkpath))
    check("symlink settings: the real target received the registrations",
          all(ev in json.loads(realfile.read_text()).get("hooks", {})
              for ev in ("PreToolUse", "SessionStart", "ConfigChange")))

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
