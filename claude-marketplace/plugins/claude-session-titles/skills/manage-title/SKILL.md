---
name: manage-session-title
description: Claude Code 会话钩子提供标题文件路径和字段锁定信息，需要随阶段或进展变化维护三段式会话标题时使用。
---

# 维护 Claude 会话标题

当钩子提供当前会话的标题文件路径，且没有要求保护整名时，在阶段或当前进展发生明显变化的这一轮尽早覆盖写入一行三段式标题。Claude Code 只允许本插件在 `SessionStart` 或 `UserPromptSubmit` 返回 `sessionTitle`；本插件不会虚构 `Stop` 实时改名能力，因此本轮写入会在下一次可用的 `UserPromptSubmit` 应用。标题仍准确时不要写。

「初始需求」概括本会话最初要解决的问题。首条提示词生成的短名只是临时值，可根据真实任务精炼一次；定稿后默认固定。「当前进展」概括现在正在做的事。中文格式为「状态｜初始需求｜当前进展」，未锁定的自动状态从「摸底、方案、开发中、测试、修复、待确认、已完成、搁置」中选择。英文格式为 `Status | Initial request | Current progress`，未锁定的自动状态从 `Scoping, Planning, Building, Testing, Fixing, Awaiting confirmation, Completed, Paused` 中选择。工作树前缀由钩子处理，不属于任何一个锁定字段，无项目会话不加目录名。

只写钩子给出的标题文件，并保留钩子列出的已锁定字段。用户在 Claude 界面修改了哪一段，钩子就锁定哪一段；人工修改并锁定的状态不受自动状态词列表限制。如果用户先修改了临时的初始需求，该段立即锁定，Agent 不得再自动定稿覆盖。钩子若说明整名保护或名称来源无法安全判断，应保留现有名称，不写文件。不要直接修改 Claude 的会话数据或在回复里添加隐藏控制标记。
