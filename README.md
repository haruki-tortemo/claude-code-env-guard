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
> This is measured behaviour, not documented behaviour — we found it by bisecting a broken config.

Any value we have not measured is treated as killing (fail-closed).

## What it warns about

Eight variables reroute Claude away from `api.anthropic.com`. Remote Control dies the same way,
but these have legitimate uses (Bedrock, Vertex, Foundry, custom gateways), so the hook
**warns at session start instead of blocking**:

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
  whatever they like to your settings. Your `settings.json` almost certainly allows `Bash(python3 *)`,
  so a model that wants to route around this hook can. We do not claim otherwise, and we will not
  add parsers chasing that — the line cannot be crossed from inside a hook.
- **It guards one specific failure mode**: an environment variable, written through the normal
  editing path, that silently disables a feature.
- **It is not a sandbox, a permission system, or a destructive-command guard.**
  If you want the latter, [dcg](https://github.com/Dicklesworthstone/destructive_command_guard)
  is a much larger and more thorough project, and it composes fine with this one.

We state this because a guard that overstates its coverage is worse than no guard —
you stop looking for the thing it claims to handle.

---

## Install

Requires **Python 3.9+** (macOS system Python works). On an older interpreter the hook
refuses to run — and blocks, rather than warns:
[docs/why-the-version-check-blocks.md](docs/why-the-version-check-blocks.md).

```bash
git clone <repo-url> && cd <repo> && python3 install/claude_code.py
```

The installer **merges** into your existing `~/.claude/settings.json`. It does not overwrite it:

- Existing hooks are preserved.
- If the file is missing, unreadable, or not valid JSON, the installer **stops and reports**
  rather than replacing it.
- A timestamped backup is written before any change.
- Re-running is idempotent.

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

**Undecidable input is not silently allowed.** A malformed payload or an internal error exits **1**
with a message — the operation proceeds, but it surfaces as a hook error instead of a silent success.

### It also guards its own registration

A guard you can quietly remove is not a guard. The hook treats **its own registration** as an
invariant: an edit that drops an event, narrows the matcher so it no longer fires, points the
command somewhere else, or replaces the file with broken JSON is refused.

`SessionStart` reads the **process environment first**, so a variable that arrived through any
path — settings, shell profile, `--settings`, managed policy — is caught even if the file looks clean.

---

## Tests

```bash
python3 tests/test_rc_env_guard.py
```

90 cases. The suite checks its own coverage against an expected-ID manifest, so deleting a case
fails the run rather than quietly shrinking the denominator.

It also **injects faults**: it disables parts of the hook and asserts the suite goes red. A test
that stays green when you break the thing it tests is not a test.

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
