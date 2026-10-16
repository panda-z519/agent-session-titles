---
name: setup-session-titles
description: 安装、迁移或排查 Codex 桌面端本地会话的三段式自动标题插件时使用。
---

# 配置会话标题插件

1. 确认用户正在本地 Codex 桌面端使用此插件。插件钩子需要本机 Python 3.11 或更新版本，以及可读取的本地 Codex 状态库。
2. 推荐按仓库根目录的 `AGENT_INSTALL.md` 执行 `npx` 安装。语言可选 `--lang zh` 或 `--lang en`；旧版用户级标题钩子需显式传 `--migrate`，安装器会先备份配置并只移除旧标题钩子的注册条目。若直接运行本插件的 `python3 scripts/setup.py`，它只准备本地配置，不完成插件安装或旧钩子迁移。
3. 安装后按 Codex 的钩子信任流程检查并信任此插件的 `SessionStart`、`UserPromptSubmit` 和 `Stop` 钩子。重启 Codex 后用新的本地会话验证。
4. 验证时读取 `<CODEX_HOME>/session-titles/<会话ID>.state.json` 的 `phase` 和 `last_result`。`source_unconfirmed` 表示当前名称来源不明，应保留名称；`io_error` 表示标题文件会留待重试。
5. 若用户准备分发插件，使用插件源目录和其 `使用说明.md`；不要把本机 `state_5.sqlite`、会话标题文件或备份打包给别人。
