# Agent Session Titles（会话标题自动更新）

为 **Codex** 和 **Claude Code** 的本地会话维护中文三段式标题：

```text
状态｜初始需求｜当前进展
开发中｜会话标题自动化｜适配双端安装器
```

中间段保留用户最初要解决的问题；状态和末段随工作变化。当前支持的状态为「摸底、方案、开发中、测试、修复、待确认、已完成、搁置」。工作树会自动加上 `[工作树] ` 前缀，无项目会话不加目录名。名称来源不明时，钩子保留现有名称。

## 安装

需要 macOS 或 Linux、Node.js 20+、Python 3.11+，以及要使用的客户端命令 `codex` 或 `claude`。选择一条命令运行：

**自动安装本机已有的客户端**（两者都有就安装两端）：

```bash
npx --yes github:panda-z519/agent-session-titles
```

**只安装 Codex：**

```bash
npx --yes github:panda-z519/agent-session-titles --codex
```

**只安装 Claude Code：**

```bash
npx --yes github:panda-z519/agent-session-titles --claude
```

**明确要求两端都安装**（缺少任一客户端命令就停止）：

```bash
npx --yes github:panda-z519/agent-session-titles --both
```

安装器从 GitHub 读取此仓库，将插件文件复制到用户目录中的稳定位置，再调用相应客户端的插件命令。它不会在 `npm install` 阶段执行 `postinstall`。新安装的 Codex 钩子需要在 Codex 中检查并信任；随后重启对应客户端，使用新的本地会话验证。[Codex 钩子信任说明](https://learn.chatgpt.com/docs/hooks)

## 安装器会修改什么

| 客户端 | 安装和配置 |
| --- | --- |
| Codex | 注册并安装 `codex-session-titles` 插件；把 `~/.codex/session-titles/` 加入窄范围可写目录；更新前检查状态库结构并创建 SQLite 一致性备份。 |
| Claude Code | 注册并安装 `claude-session-titles` 插件；在 `~/.claude/settings.json` 中加入 `Edit(~/.cc-session-titles/**)`；改动前备份已有设置。 |

已安装旧版用户级 `session-title.sh` 的客户端会被**跳过**，原有钩子、配置和会话数据保留，以免同一事件运行两次。安装器会明确打印跳过原因。`CODEX_HOME` 和 `CLAUDE_CONFIG_DIR` 分别支持自定义两端配置目录。

## 验证标题

1. 重启客户端，新建一个**本地会话**，提出具体需求并让助手完成首轮实质回复。
2. 标题应呈现「状态｜初始需求｜当前进展」。继续沟通并明显改变阶段后，状态或末段应更新，中间段保持原样。
3. Codex 在 `Stop` 同步标题；若仍有待处理标题文件，下一次发消息时会补偿更新。可检查 `~/.codex/session-titles/<会话ID>.state.json` 的 `phase`、`last_result`。
4. Claude Code 使用官方 `sessionTitle` 钩子输出；本轮回复写入的标题会在**下一次提交消息时**应用。可检查 `~/.cc-session-titles/<会话ID>.txt`。[Claude 钩子说明](https://code.claude.com/docs/en/hooks)

历史会话在收到新消息后才重新纳入 Codex 标题管理。若钩子提示“来源未确认”，会保留已有名称。无项目会话使用需求摘要，不把当前目录冒充项目名。

## 更新与卸载

更新时重新运行相同的 `npx` 命令，并重启对应客户端。安装器可重复执行；Codex 新版本会另存状态库备份。插件内部 ID 沿用 `codex-session-titles` 和 `claude-session-titles`，方便已安装用户升级。

卸载插件可分别运行：

```bash
codex plugin remove codex-session-titles@codex-session-titles-marketplace
claude plugin uninstall claude-session-titles@session-titles
```

只使用一端时运行该端的卸载命令。准备脚本添加的可写目录或 `Edit` 规则可从用户配置中单独移除；旧版独立钩子和备份不会被自动删除。

## 限制

- Codex 标题同步会访问本机 `state_5.sqlite`，属于依赖当前数据库结构的本地集成；结构不符时准备脚本会停止配置，钩子不会创建空数据库。
- Claude Code 的标题更新发生在下一次 `SessionStart` 或 `UserPromptSubmit`，不能在本轮回复结束时由官方钩子立即改名。
- 本仓库的生命周期钩子面向本地桌面端，不能作为带钩子的插件提交到 OpenAI 公共插件目录。[插件打包说明](https://developers.openai.com/plugins/build/plugins)

手动安装、隔离检查与排查步骤见[使用说明](使用说明.md)。源码按 [MIT 许可证](LICENSE)开放。
