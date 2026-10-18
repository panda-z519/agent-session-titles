# Agent Session Titles

[简体中文](README.md) | **English**

Automatically maintain three-part titles for local **Codex** and **Claude Code** sessions:

```text
Status | Initial request | Current progress
Building | Session titles | Add bilingual support
```

The initial request stays stable. Status and progress change at meaningful phase transitions. If you manually edit one segment, the plugin preserves that segment while continuing to update the others. Manually chosen status words do not have to match the automatic status list. Worktrees receive a `[worktree]` prefix; sessions without a project do not gain a made-up project name. A title whose source or three-part structure cannot be confirmed is preserved as a whole.

## Install with an Agent

Send the [English Agent installation instructions](AGENT_INSTALL.md#english-instructions) to a Codex or Claude Code Agent with local terminal access. The Agent can check prerequisites, run the installer, inspect any legacy-hook migration, and report the result. You do not need to run terminal commands yourself. Codex may still ask you to review and trust the new hooks in the app.

## Install from your terminal

Requires macOS or Linux, Node.js 20+, Python 3.11+, and the client command you want to use (`codex` or `claude`). Choose a target:

```bash
# Auto-detect installed clients; install both when both are available.
npx --yes github:panda-z519/agent-session-titles

# Codex only.
npx --yes github:panda-z519/agent-session-titles --codex

# Claude Code only.
npx --yes github:panda-z519/agent-session-titles --claude

# Require both clients.
npx --yes github:panda-z519/agent-session-titles --both
```

New installations default to Chinese. Select English with `--lang en`, or switch back with `--lang zh`. Rerunning the installer without `--lang` keeps the saved choice:

```bash
npx --yes github:panda-z519/agent-session-titles --codex --lang en
npx --yes github:panda-z519/agent-session-titles --claude --lang en
```

If an older standalone `session-title.sh` hook is registered, add `--migrate`. The installer backs up the relevant configuration and removes only matching legacy title-hook registrations; it keeps the old script, unrelated hooks, and session data. Without `--migrate`, installation stops with an explanation. Personal title rules in global `AGENTS.md` or `CLAUDE.md` may also need updating if they require another language or only allow end-of-turn updates. The Agent installation instructions include that check.

### Global rules files

The plugin hooks inject the title rules into every session, so you do **not** need to write them in global `AGENTS.md` (Codex) or `CLAUDE.md` (Claude Code). By default the installer leaves these files unchanged. It scans them for lines that look like handwritten session-title rules and reports their line numbers, so you can review possible duplicates or conflicts.

To also keep the rules in your global rules files, add `--rules`. The installer appends a managed block wrapped in `<!-- agent-session-titles:start -->` and `<!-- agent-session-titles:end -->`, generated in the selected title language, after backing up the file. Later reinstalls refresh an existing block even without `--rules`. To remove the managed block without installing anything:

```bash
npx --yes github:panda-z519/agent-session-titles --remove-rules
```

`--remove-rules` also accepts `--codex` or `--claude`. It removes only the content between the markers and keeps the rest of the file.

The installer copies plugin files to a stable directory under the user's Codex configuration directory and invokes each client's plugin commands. It has no npm `postinstall` script. Restart the affected client after installation. Codex will ask you to [review and trust non-managed hooks](https://learn.chatgpt.com/docs/hooks), including new hook definitions after an update.

## What the installer changes

| Client | Configuration |
| --- | --- |
| Codex | Registers `codex-session-titles`, grants narrowly scoped write access to `~/.codex/session-titles/`, checks the local SQLite state schema, and makes a consistent database backup before configuration changes. |
| Claude Code | Registers `claude-session-titles` and adds `Edit(~/.cc-session-titles/**)` to `~/.claude/settings.json`, backing up existing settings before editing them. |

`CODEX_HOME` and `CLAUDE_CONFIG_DIR` select non-default client configuration directories.

## Verify the behavior

1. Restart the client and start a **new local session** with a specific request. The Agent should form a three-part title after understanding the request, then update status or progress as the work changes.
2. Edit one title segment manually and move the work to another phase. Your edited segment should remain intact while unlocked segments continue to update. An unparseable custom title should remain untouched.
3. In Codex, the plugin tries to apply a title after the tool call that writes its title file. `Stop` and the next prompt provide fallback opportunities. Inspect `~/.codex/session-titles/<session-id>.state.json` for `phase`, `last_result`, and segment locks. When the Codex CLI uses the background app-server daemon, the plugin calls `thread/name/set` through its control socket, so an open CLI window refreshes its status-line title immediately. If the daemon is unavailable (for example with `--no-daemon`), the plugin falls back to writing SQLite directly, and an open window shows the new title only after it is reopened or after `/rename`. A database update alone does not prove the desktop sidebar refreshed immediately.
4. In Claude Code, the first `UserPromptSubmit` can provide a provisional initial title. In the **desktop app**, a `PostToolUse` hook renames the session **immediately** after the Agent writes the title file. In the **CLI**, where that desktop tool does not exist, progress applies on a subsequent `UserPromptSubmit` (the `PostToolUse` hook reports a non-blocking error), subject to [Claude's supported hook output](https://code.claude.com/docs/en/hooks). Inspect `~/.cc-session-titles/<session-id>.txt` and the session state file when troubleshooting.

Historical Codex sessions re-enter title handling after a new user message. Sessions without a reliable plugin baseline may remain protected. The [Chinese usage guide](使用说明.md) has additional manual installation and troubleshooting details.

## Update and uninstall

Rerun the appropriate `npx` command to update, then restart the client. Plugin names stay `codex-session-titles` and `claude-session-titles`; both plugin sources are now named `agent-session-titles`. When upgrading from the old source names, the installer uninstalls the old plugins, removes the old source registrations, and deletes the old copied directories after the new plugins are installed, so hooks do not run twice and no manual cleanup is needed. To uninstall either client:

```bash
codex plugin remove codex-session-titles@agent-session-titles
claude plugin uninstall claude-session-titles@agent-session-titles
```

Run only the command for the client you installed. You can remove the write-access configuration separately. Existing session data and backups are kept.

## Limits

- Codex renames go through the local background app-server's `thread/name/set` first and fall back to writing the local `state_5.sqlite` database. A future Codex schema change may require an update; the setup script checks required columns and does not create a fake database. Sidebar refresh timing depends on the desktop client.
- In the Claude Code CLI, an Agent-written progress change waits for the next `UserPromptSubmit`. Immediate renaming relies on the desktop app's built-in `ccd_session_mgmt` tool.
- Immediate renaming applies the title file as written; a locked part that gets overwritten is restored on the next prompt. It does not work with a custom `CC_TITLE_DIR`, because the hook condition matches `~/.cc-session-titles/`.
- A difference from the last title applied by the plugin proves an **external edit**, not who made it. Before a baseline exists, the plugin cannot guarantee that it can identify prior manual edits.
- Lifecycle-hook plugins are intended for local desktop installation and [cannot be submitted with hooks to OpenAI's public plugin directory](https://developers.openai.com/plugins/build/plugins).

Released under the [MIT License](LICENSE).
