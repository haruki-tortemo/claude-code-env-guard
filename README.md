# rc-env-guard

**A Claude Code hook that stops you from silently killing Remote Control with an environment variable.**

Put `DO_NOT_TRACK=1` in your `settings.json` and Claude Code's Remote Control stops working.
No error. No warning. `remoteControlAtStartup: true` is still there, the setting looks correct,
and your phone just never connects again.

We lost three days to this before finding it. This hook is the thing we wish had existed.

---

## What it blocks

Four variables kill Remote Control. The value semantics are **not** what you would guess:

| Variable | `"1"` | `"0"` | empty |
|---|---|---|---|
| `DO_NOT_TRACK` | kills | safe | safe |
| `DISABLE_GROWTHBOOK` | kills | safe | safe |
| `DISABLE_TELEMETRY` | kills | **kills** | safe |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | kills | **kills** | safe |

> **`DISABLE_TELEMETRY=0` still kills it.** Setting it to "off" is not the same as not setting it.
> This is measured behavior, not documented behavior — we found it by bisecting a broken config.

Any value we have not measured is treated as killing (fail-closed).

What happens depends on the event that catches it:

| Event | What the guard does with these four |
|---|---|
| `PreToolUse` | blocks the write before it lands |
| `ConfigChange` | blocks applying the already-written change to the current session |
| `SessionStart` | warns only (startup is never blocked) |

## What it warns about

Eight variables change the endpoint, transport, provider, or authentication mode in ways that
make Remote Control unavailable. They have legitimate uses (Bedrock, Vertex, Foundry, custom
gateways), so the hook **warns at session start instead of blocking**:

`ANTHROPIC_API_KEY` · `ANTHROPIC_BASE_URL` · `ANTHROPIC_UNIX_SOCKET` ·
`CLAUDE_CODE_USE_BEDROCK` · `CLAUDE_CODE_USE_VERTEX` · `CLAUDE_CODE_USE_FOUNDRY` ·
`CLAUDE_CODE_USE_ANTHROPIC_AWS` · `CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD`

Claude Code's own message when you try:
`"Remote Control is only available when using Claude via api.anthropic.com"`

Blocking these would train you to route around the hook. Warning is the honest level.

---

## 🔴 What this does NOT do

**Read this before installing. The guarantee is deliberately narrow.**

> **This hook prevents accidents. It does not prevent deliberate circumvention.**

Concretely:

- **Arbitrary code execution is out of scope.** `python3 -c "..."` and `python3 - <<EOF` can write
  whatever they like to your settings. If Python execution is allowed in your permission settings,
  a script can write the settings file in ways this hook cannot reliably inspect. We do not claim
  otherwise, and we will not add parsers chasing that — the line cannot be crossed from inside a hook.
- **It guards one specific failure mode**: an environment variable, written through the normal
  editing path, that silently disables a feature.
- **It is not a sandbox, a permission system, or a destructive-command guard.**
  For broader destructive-command protection, see
  [dcg](https://github.com/Dicklesworthstone/destructive_command_guard) — it addresses a
  different and much larger problem space. Both installers preserve existing hooks, but this
  project does not test the two together.

We state this because a guard that overstates its coverage is worse than no guard —
you stop looking for the thing it claims to handle.

### Known ways a determined actor gets past it

"Deliberate circumvention" above is not hand-waving. These are specific, known bypasses.
They exist because a hook is a single Python process reacting to one tool call at a time — it
cannot be a security boundary against an actor (including a prompt-injected model) that is
actively working around it. If your threat model includes that, this hook is not your control.

- **Equivalent spellings of the settings path.** The write-time check normalizes the path and
  falls back to real-inode identity, so `~/.claude//settings.json`, `~/.claude/./settings.json`,
  and a symlink to the settings file are caught, as is a case variant like `~/.claude/Settings.json`
  **when the file already exists** on a case-insensitive volume. Still out of scope: a case variant
  of a settings file that does not exist yet, and native Windows `\` paths.
- **Environment-based interpreter tricks.** A settings write that sets `PATH` or `PYTHONPATH`
  (neither is on the blocked list) can stop the hook from running, or make it import attacker
  Python — without touching the `hooks` block.
- **Editing the hook's own file.** The guard does not protect `~/.claude/hooks/rc_env_guard.py`;
  overwriting or deleting it disarms the guard while `settings.json` still shows it registered.
- **Deleting or moving settings.json via Bash** (`rm`, `mv` to elsewhere) removes the
  registration; only writes *into* the file are judged.
- **Registration forms the recognizer doesn't know** (a version-suffixed interpreter it hasn't
  been taught, a matcher spelled with commas or spaces) leave the self-registration invariant off.
- **Crafted, very long commands** can make the Bash regexes slow enough to hit the hook timeout.

The value it does provide is against the *accident*: a telemetry opt-out pasted into settings by a
human or an unsuspecting model, through the ordinary editing path. That is the case it opened with,
and the one it reliably catches.

---

## Install

Requires **Python 3.9+** (macOS system Python works). On an older interpreter the hook
refuses to run — and blocks, rather than warns:
[docs/why-the-version-check-blocks.md](docs/why-the-version-check-blocks.md).

```bash
git clone https://github.com/haruki-tortemo/claude-code-env-guard.git
cd claude-code-env-guard
python3 install/claude_code.py --dry-run
python3 install/claude_code.py
```

The installer copies the hook to a stable location next to your settings file
(`~/.claude/hooks/rc_env_guard.py`) and registers that copy — you can move or delete the
checkout afterwards. It **merges** into your existing `~/.claude/settings.json`. It does not
overwrite it:

- Existing hooks are preserved.
- If the file is missing, unreadable, or not valid JSON, the installer **stops and reports**
  rather than replacing it.
- A uniquely named backup is written before any change (two runs in the same second cannot
  clobber each other's backups).
- Re-running is idempotent, and a half-broken earlier registration (a narrowed matcher,
  `async: true`, a dropped event) is repaired rather than accepted.
- `--settings` only accepts `.claude/settings.json` / `.claude/settings.local.json` — the
  only paths the guard actually watches. Installing anywhere else would report success while
  guarding nothing, so it is refused.

Restart Claude Code afterwards.

### Uninstall

```bash
python3 install/claude_code.py --uninstall
```

---

## How it decides

The hook registers on three events, and **needs all three**:

| Event | Why |
|---|---|
| `PreToolUse` (`Edit`/`Write`/`MultiEdit`/`Bash`) | catch the write before it lands |
| `SessionStart` | catch variables that arrived some other way (shell, `--settings`, managed config) |
| `ConfigChange` | catch changes the hook did not mediate |

To block, it exits **2** with the reason on `stderr`. That is Claude Code's documented contract:
exit 2 discards stdout and returns stderr to the model.

**Undecidable input is not silently allowed.** For `PreToolUse`, malformed input or an internal
evaluation error exits **1** and surfaces as a non-blocking hook error. `SessionStart` and
`ConfigChange` are observational backstops: inspection failures are reported, but startup
continues and a configuration change may already be on disk.

### It resists the common ways its registration gets dropped

On the ordinary Edit/Write path, the hook treats **its own registration** as an invariant: an
edit that drops an event, narrows the matcher to a form it recognizes as no longer firing, points
the command at a different script, or replaces the file with broken JSON is refused. This is
aimed at the accidental or structural case, not at a determined actor — the
[known bypasses](#known-ways-a-determined-actor-gets-past-it) above (editing the hook's own file,
env/interpreter tricks, deleting settings via Bash, unrecognized registration forms) are real and
are not caught here. The invariant reduces how easily the guard is *accidentally* removed; it does
not make it tamper-proof.

`SessionStart` reads the **process environment first**, so a variable that arrived through any
path — settings, shell profile, `--settings`, managed policy — is caught even if the file looks clean.

---

## Tests

```bash
python3 tests/test_rc_env_guard.py   # 98 cases: the guard's decision contract
python3 tests/test_compat.py         #  9 cases: the interpreter-floor contract
python3 tests/test_installer.py      # 63 cases: the installer, incl. spaces/metachars
                                     #           in the path, repair, backups,
                                     #           co-located hooks, move-then-uninstall
```

The main suite checks its own coverage against an expected-ID manifest, so deleting a case
fails the run rather than quietly shrinking the denominator. CI runs all three suites on
Python 3.9 and a current Python.

The suites also **inject faults**: they disable parts of the hook or installer and assert the
run goes red. A test that stays green when you break the thing it tests is not a test.

### What we learned building this (and got wrong twice)

Both of these were real holes in this hook, found after it was already "done":

1. **Line continuations.** `\` + newline split the command into segments before analysis, so
   `printf '{}' >\` ⏎ `~/.claude/settings.json` reached neither check and passed. Four shapes bypassed.
2. **Then the fix for #1 introduced three more.** Folding `\`+newline into a *space* makes
   `~/.claude/sett\` ⏎ `ings.json` read as two words. Bash **removes** the sequence; it does not
   replace it. Replacing with a space is a false negative generator.

The second one is the interesting one: relaxing a rule to stop a false positive is, by definition,
weakening detection. If you do it, write the attack the relaxation now permits — in the same commit.

---

## License

MIT. See [LICENSE](LICENSE).
