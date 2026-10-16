# Agent Session Titles

Automatically maintain three-part titles for local Codex and Claude Code sessions:

```text
Status | Initial request | Current progress
Building | Session titles | Add bilingual support
```

The initial request stays stable. The status and progress change when the work changes. Chinese titles use `状态｜初始需求｜当前进展`. Worktrees get a `[worktree]` or `[工作树]` prefix. Sessions without a project use the request summary without adding a directory name.

## Let your Agent install it

Copy the [English Agent instructions](AGENT_INSTALL.md#english-instructions) into a Codex or Claude Code session with local terminal access. The Agent runs the installer and checks the result. You may still need to review and trust new Codex hooks in the app.

## Run the installer yourself

Requires macOS or Linux, Node.js 20+, Python 3.11+, and the `codex` and/or `claude` command:

```bash
npx --yes github:panda-z519/agent-session-titles --codex --lang en
npx --yes github:panda-z519/agent-session-titles --claude --lang en
npx --yes github:panda-z519/agent-session-titles --both --lang en
```

Without a target flag, the installer detects available clients. New installs default to Chinese when `--lang` is omitted; reruns preserve the saved choice. Use `--lang zh` to switch to Chinese. If legacy standalone session title hooks exist, add `--migrate` to back up settings and remove only their registrations before installing the plugin. Personal title rules in global `AGENTS.md` or `CLAUDE.md` may also need to be updated when switching languages; the Agent installation prompt covers this check.

Restart the client after installation. Codex updates titles at `Stop`, with a retry on the next prompt when needed. Claude Code applies a title on the next `UserPromptSubmit` event after the Agent writes the title file; `SessionStart` supplies writing guidance. Existing names of uncertain origin are preserved. More details, verification, and uninstall commands are in the [Chinese README](README.md) and [usage guide](使用说明.md).

Source code is available under the [MIT license](LICENSE).
