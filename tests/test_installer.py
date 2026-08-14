#!/usr/bin/env python3
"""Installer contract tests, with fault injection.

The installer edits a file the user cares about more than anything else we ship.
The cases that matter are the broken ones: a settings file we cannot parse must
leave the disk untouched, not get "repaired" into a fresh file.

Every case runs against a fixture in a temp dir. The real ~/.claude/settings.json
is never passed to the installer here.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install" / "claude_code.py"

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


def run(settings: pathlib.Path, *extra: str):
    proc = subprocess.run(
        [sys.executable, str(INSTALLER), "--settings", str(settings), *extra],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def fixture(tmp: pathlib.Path, content, name="settings.json") -> pathlib.Path:
    path = tmp / name
    if content is not None:
        path.write_text(content if isinstance(content, str)
                        else json.dumps(content, indent=2), encoding="utf-8")
    return path


results = []


def check(label: str, ok: bool, detail: str = ""):
    results.append((label, ok))
    print(("PASS " if ok else "FAIL ") + label + (("  -- " + detail) if detail and not ok else ""))


with tempfile.TemporaryDirectory() as td:
    tmp = pathlib.Path(td)

    # --- refuses to touch what it cannot understand -------------------------
    missing = tmp / "nope" / "settings.json"
    code, out = run(missing)
    check("missing file: aborts, does not create", code == 1 and not missing.exists(), out[:80])

    broken = fixture(tmp, '{"hooks": {, }', name="broken.json")
    before = broken.read_text()
    code, out = run(broken)
    check("invalid JSON: aborts with line/column", code == 1 and "line" in out, out[:80])
    check("invalid JSON: file byte-identical afterwards", broken.read_text() == before)
    check("invalid JSON: no backup left behind",
          not list(tmp.glob("broken.json.backup-*")))

    notobj = fixture(tmp, '["a", "b"]', name="array.json")
    code, out = run(notobj)
    check("top-level array: aborts", code == 1 and notobj.read_text() == '["a", "b"]')

    badhooks = fixture(tmp, '{"hooks": "not-an-object"}', name="badhooks.json")
    code, out = run(badhooks)
    check("hooks is not an object: aborts", code == 1 and "not an object" in out)

    # --- dry run ------------------------------------------------------------
    dry = fixture(tmp, EXISTING, name="dry.json")
    before = dry.read_text()
    code, out = run(dry, "--dry-run")
    check("dry-run: reports 3 events", code == 0 and out.count("added ") == 3, out[:80])
    check("dry-run: writes nothing", dry.read_text() == before)

    # --- the happy path preserves everything --------------------------------
    live = fixture(tmp, EXISTING, name="live.json")
    code, out = run(live)
    data = json.loads(live.read_text())
    check("install: exit 0", code == 0, out[:80])
    check("install: unrelated top-level keys survive",
          data.get("model") == "opus" and data["env"]["SOME_USER_VAR"] == "1")
    check("install: pre-existing PreToolUse hook survives",
          any(h.get("command") == "/usr/local/bin/other-guard"
              for e in data["hooks"]["PreToolUse"] for h in e.get("hooks", [])))
    check("install: pre-existing unrelated event survives", "Stop" in data["hooks"])
    check("install: all three events registered",
          all(ev in data["hooks"] for ev in ("PreToolUse", "SessionStart", "ConfigChange")))
    check("install: backup written", bool(list(tmp.glob("live.json.backup-*"))))

    backup_count = len(list(tmp.glob("live.json.backup-*")))
    code, out = run(live)
    check("install twice: idempotent (no duplicate entry)",
          code == 0 and "Nothing to do" in out, out[:80])
    check("install twice: no second backup for a no-op",
          len(list(tmp.glob("live.json.backup-*"))) == backup_count)

    # --- uninstall removes ours and only ours --------------------------------
    code, out = run(live, "--uninstall")
    data = json.loads(live.read_text())
    check("uninstall: exit 0", code == 0, out[:80])
    check("uninstall: our entries gone",
          not any("rc_env_guard" in h.get("command", "")
                  for e in data["hooks"].get("PreToolUse", []) for h in e.get("hooks", [])))
    check("uninstall: other guard untouched",
          any(h.get("command") == "/usr/local/bin/other-guard"
              for e in data["hooks"]["PreToolUse"] for h in e.get("hooks", [])))
    check("uninstall: unrelated event untouched", "Stop" in data["hooks"])

    # --- fault injection: does the suite actually detect a destructive install?
    repo = tmp / "repo"
    shutil.copytree(ROOT, repo)
    mutant = repo / "install" / "claude_code.py"
    src = mutant.read_text()
    # Mutation: silently treat broken JSON as {} - the behaviour that would
    # rebuild the user's settings from scratch.
    mutated = src.replace(
        "    except json.JSONDecodeError as exc:\n",
        "    except json.JSONDecodeError as exc:\n        return {}\n", 1)
    assert mutated != src, "mutation did not apply -- the test would be vacuous"
    mutant.write_text(mutated)
    broken2 = fixture(tmp, '{"hooks": {, }', name="broken2.json")
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
