# Codex 与 Claude 三段式会话标题插件源

这个安装包为 Codex 和 Claude Code 的本地会话提供「状态｜初始需求｜当前进展」标题。从 GitHub 仓库运行一条命令即可安装：

```bash
npx --yes --package=github:panda-z519/codex-session-titles codex-session-titles
```

安装器会检测本机可用的 Codex 和 Claude Code，配置对应插件；已有的独立命名钩子会保留，避免重复执行。新安装的 Codex 插件首次使用仍需检查并信任钩子。项目采用 [MIT 许可证](LICENSE)。安装要求和卸载方法见[使用说明](使用说明.md)。

Claude Code 端的标题会在下一次提交消息时应用，因为官方只在 `SessionStart` 和 `UserPromptSubmit` 接受标题钩子输出。
