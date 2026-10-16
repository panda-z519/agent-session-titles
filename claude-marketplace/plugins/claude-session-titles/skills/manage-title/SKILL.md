---
name: manage-session-title
description: Claude Code 会话钩子提供标题文件路径，需要在首次实质回复或阶段变化时维护三段式会话标题时使用。
---

# 维护 Claude 会话标题

当钩子提供当前会话的标题文件路径，且没有要求保护现有名称时，首次实质回复后按钩子当前语言提示覆盖写入一行三段式标题；之后只在阶段或主题明显变化时更新。标题仍准确时不要写。

「初始需求」概括本会话最初要解决的问题，后续保持不变；「当前进展」概括现在正在做的事。中文格式为「状态｜初始需求｜当前进展」，状态从「摸底、方案、开发中、测试、修复、待确认、已完成、搁置」中选择。英文格式为 `Status | Initial request | Current progress`，状态从 `Scoping, Planning, Building, Testing, Fixing, Awaiting confirmation, Completed, Paused` 中选择。工作树前缀由钩子处理，无项目会话不加目录名。

只写钩子给出的标题文件。钩子若说明名称来源未确认，应保留现有名称，不写文件。不要直接修改 Claude 的会话数据或在回复里添加隐藏控制标记。
