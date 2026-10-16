# 交给 Agent 安装 / Install with an Agent

将下面对应的文字发给有本机终端权限的 Agent。Agent 可以替你运行命令；Codex 首次加载插件钩子时，应用仍可能要求你亲自检查并信任。[官方钩子说明](https://learn.chatgpt.com/docs/hooks)

## 中文指令

> 请安装 https://github.com/panda-z519/agent-session-titles 的会话标题插件。目标客户端是 **Codex 和 Claude Code**，标题语言是 **中文**。先检查 `codex`、`claude`、Node.js 20+ 和 Python 3.11+ 是否可用，再运行 `npx --yes github:panda-z519/agent-session-titles --both --lang zh --migrate`。如果只需 Codex，把 `--both` 改成 `--codex`；只需 Claude Code，改成 `--claude`。`--migrate` 会备份配置并只移除旧标题钩子的注册条目。检查全局 `AGENTS.md`、`CLAUDE.md` 是否仍有相冲突的旧标题语言规则；只对明确属于本标题功能的规则备份后更新，无法安全识别时向我报告。安装后检查输出、语言配置与旧钩子迁移结果；失败时给出具体原因和已完成的步骤。提醒我重启客户端，并在 Codex 要求时让我亲自检查并信任新钩子。不要把隔离环境通过当成真实桌面端生效的证据。

## English instructions

> Install the session title plugin from https://github.com/panda-z519/agent-session-titles for **Codex and Claude Code**, with **English** titles. Check that `codex`, `claude`, Node.js 20+, and Python 3.11+ are available. Run `npx --yes github:panda-z519/agent-session-titles --both --lang en --migrate`. For Codex only, replace `--both` with `--codex`; for Claude Code only, use `--claude`. The `--migrate` option backs up settings and removes only legacy session title hook registrations. Check global `AGENTS.md` and `CLAUDE.md` for conflicting title language rules; back up and update only rules clearly belonging to this title feature, and report any rule that cannot be safely identified. Check the command output, saved language selection, and migration result. If installation fails, report the exact reason and what completed. Tell me to restart the clients and let me review and trust Codex hooks if the app asks. Do not claim that an isolated test proves the desktop title has changed.

## 语言与目标 / Language and target

| 参数 | 作用 |
| --- | --- |
| `--codex` | 仅安装 Codex / Codex only |
| `--claude` | 仅安装 Claude Code / Claude Code only |
| `--both` | 同时安装两个客户端 / both clients |
| `--lang zh` | 中文标题 / Chinese titles |
| `--lang en` | 英文标题 / English titles |
| `--migrate` | 备份并迁移旧标题钩子 / back up and migrate legacy title hooks |

首次安装不指定 `--lang` 时使用中文；再次运行不指定时保留已选语言。安装器只适用于运行在用户本机、具有终端和文件访问能力的 Agent。远端或纯聊天 Agent 无法替用户安装本地插件。
