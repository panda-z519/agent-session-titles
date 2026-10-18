#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const PRODUCT_NAME = "agent-session-titles";
const CODEX_MARKETPLACE_NAME = PRODUCT_NAME;
const CODEX_PLUGIN_NAME = "codex-session-titles";
const CLAUDE_MARKETPLACE_NAME = PRODUCT_NAME;
const CLAUDE_PLUGIN_NAME = "claude-session-titles";
// 0.6.0 之前的插件源名称与复制目录；升级时自动卸载，避免新旧钩子重复运行
const LEGACY_CODEX_MARKETPLACE = { name: "codex-session-titles-marketplace", directory: "codex-session-titles" };
const LEGACY_CLAUDE_MARKETPLACE = { name: "session-titles", directory: "session-titles" };
// 写入全局规则文件的托管块：起止标记之间的内容归安装器所有，重装时整体替换
const RULES_START = "<!-- agent-session-titles:start -->";
const RULES_END = "<!-- agent-session-titles:end -->";
const RULES_TEXT = {
  zh: [
    "<!-- 由 agent-session-titles 安装器维护；重新安装时整块替换，--remove-rules 时整块删除。 -->",
    "## 会话标题（agent-session-titles 插件）",
    "",
    "- 会话名采用「状态｜初始需求｜当前进展」；状态从 摸底、方案、开发中、测试、修复、待确认、已完成、搁置 中选一个。",
    "- 中间段概括用户最初要解决的问题，确定后保持不变；末段随阶段或进展变化更新。",
    "- 标题文件路径和当前名称由插件钩子在会话上下文中提供。理解初始需求后，以及阶段或主题明显变化时，用写入文件的工具覆盖写入一行三段式标题；标题仍准确时不要写。",
    "- 钩子没有提供标题文件路径，或提示当前名称来源未确认时，保留现有名称，不写标题文件。",
    "- 本节与钩子注入的提示不一致时，以钩子提示为准。",
  ],
  en: [
    "<!-- Maintained by the agent-session-titles installer; replaced on reinstall and removed by --remove-rules. -->",
    "## Session titles (agent-session-titles plugin)",
    "",
    "- Session names use \"Status | Initial request | Current progress\". Choose Status from Scoping, Planning, Building, Testing, Fixing, Awaiting confirmation, Completed, or Paused.",
    "- The middle part captures the user's initial request and stays unchanged once set; the final part follows the current phase or progress.",
    "- The plugin hooks provide the title file path and the current name in the session context. After understanding the initial request, and whenever the phase or topic changes significantly, use a file-writing tool to overwrite that file with one three-part line; do not rewrite an accurate title.",
    "- If the hooks provide no title file path, or say the current name's source is unconfirmed, keep the existing name and do not write the title file.",
    "- If this section disagrees with the hook-provided instructions, follow the hooks.",
  ],
};
// 托管块之外疑似手写标题规则的特征，只用于提示，不据此修改文件
const TITLE_RULE_PATTERNS = [
  /状态｜初始需求｜当前进展/,
  /status\s*\|\s*initial request\s*\|\s*current progress/i,
  /会话标题|会话命名|会话名/,
  /session[ -]?(?:title|naming|name)/i,
  /cc-session-titles|session-titles/,
];

const useColor = Boolean(process.stdout.isTTY) && !process.env.NO_COLOR;
const paint = (code) => (text) => (useColor ? `\x1b[${code}m${text}\x1b[0m` : String(text));
const bold = paint("1");
const dim = paint("2");
const red = paint("31");
const green = paint("32");
const yellow = paint("33");
const cyan = paint("36");

// 当前步骤收集的补充说明；不在步骤内时直接输出
let pendingNotes = null;

function displayPath(value) {
  const home = os.homedir();
  return value === home || value.startsWith(`${home}${path.sep}`) ? `~${value.slice(home.length)}` : value;
}

function note(text) {
  if (pendingNotes) {
    pendingNotes.push(text);
  } else {
    console.log(`    ${dim(`· ${text}`)}`);
  }
}

function printStepLine(mark, label, hint) {
  const padded = hint ? `${label}${" ".repeat(Math.max(2, 14 - visualWidth(label)))}` : label;
  console.log(`  ${mark} ${padded}${hint ? dim(hint) : ""}`);
}

// 中文字符在终端占两列，按显示宽度对齐提示列
function visualWidth(text) {
  return [...text].reduce((width, char) => width + (char.charCodeAt(0) > 0xff ? 2 : 1), 0);
}

function step(label, action) {
  pendingNotes = [];
  let result;
  try {
    result = action() || {};
  } catch (error) {
    const notes = pendingNotes;
    pendingNotes = null;
    printStepLine(red("✗"), label);
    for (const text of notes) note(text);
    for (const line of error.output || []) console.log(`    ${dim(line)}`);
    throw error;
  }
  const notes = pendingNotes;
  pendingNotes = null;
  printStepLine(result.warning ? yellow("!") : green("✓"), label, result.hint);
  for (const text of notes) note(text);
  if (result.warning) console.log(`    ${yellow(result.warning)}`);
  return result;
}

function section(title) {
  console.log(`\n  ${bold(title)}`);
}

function printHelp() {
  console.log(`Agent Session Titles：Codex 与 Claude Code 会话标题插件安装器

用法：
  npx --yes github:panda-z519/agent-session-titles
  npx --yes github:panda-z519/agent-session-titles --codex
  npx --yes github:panda-z519/agent-session-titles --claude
  npx --yes github:panda-z519/agent-session-titles --both
  npx --yes github:panda-z519/agent-session-titles --both --lang en

选项：
  --lang zh|en  标题语言；首次安装默认中文，更新时保留上次选择
  --migrate     备份配置并迁移旧版用户级标题钩子
  --rules       在全局 AGENTS.md / CLAUDE.md 末尾写入（或更新）带标记的标题规则块
  --remove-rules  只删除安装器写入的标题规则块，不安装插件

默认不修改全局规则文件，只提示其中可能与插件规则重复或冲突的行；
已存在的托管规则块会随重装自动更新。

环境变量：
  CODEX_HOME  Codex 配置目录，默认使用 ~/.codex
  CLAUDE_CONFIG_DIR  Claude Code 配置目录，默认使用 ~/.claude

安装器会检测 PATH 中的 codex 和 claude，至少安装一个可用客户端的插件。
若已配置旧版用户级标题钩子，需使用 --migrate 才会迁移到插件。`);
}

function resolveConfigDirectory(configured, fallback) {
  if (!configured) {
    return fallback;
  }
  if (configured === "~") {
    return os.homedir();
  }
  if (configured.startsWith(`~${path.sep}`)) {
    return path.join(os.homedir(), configured.slice(2));
  }
  return path.resolve(configured);
}

function commandExists(command) {
  const pathValue = process.env.PATH || "";
  return pathValue.split(path.delimiter).some((directory) => {
    const candidate = path.join(directory || process.cwd(), command);
    try {
      if (!fs.statSync(candidate).isFile()) {
        return false;
      }
      fs.accessSync(candidate, fs.constants.X_OK);
      return true;
    } catch {
      return false;
    }
  });
}

function parseOptions(args) {
  let target = "auto";
  let language = null;
  let migrate = false;
  let rules = false;
  let removeRules = false;
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (["--codex", "--claude", "--both"].includes(argument)) {
      if (target !== "auto") {
        throw new Error("--codex、--claude 和 --both 不能重复或组合使用。");
      }
      target = argument.slice(2);
    } else if (argument === "--lang") {
      if (language !== null || !["zh", "en"].includes(args[index + 1])) {
        throw new Error("--lang 只能指定一次，取值为 zh 或 en。");
      }
      language = args[index + 1];
      index += 1;
    } else if (argument === "--migrate") {
      if (migrate) {
        throw new Error("--migrate 不能重复指定。");
      }
      migrate = true;
    } else if (argument === "--rules" || argument === "--remove-rules") {
      if (rules || removeRules) {
        throw new Error("--rules 和 --remove-rules 只能指定其中一个，且不能重复。");
      }
      rules = argument === "--rules";
      removeRules = argument === "--remove-rules";
    } else {
      throw new Error(`不支持的参数：${argument}。使用 --help 查看用法。`);
    }
  }
  if (removeRules && (language !== null || migrate)) {
    throw new Error("--remove-rules 只删除规则块，不能与 --lang 或 --migrate 同时使用。");
  }
  return { target, language, migrate, rules, removeRules };
}

function requireSource(sourcePath, expectedType, description) {
  let stats;
  try {
    stats = fs.statSync(sourcePath);
  } catch (error) {
    throw new Error(`安装包缺少${description}：${sourcePath}（${error.message}）`);
  }
  const valid = expectedType === "file" ? stats.isFile() : stats.isDirectory();
  if (!valid) {
    throw new Error(`安装包中的${description}类型不正确：${sourcePath}`);
  }
}

function copyCodexMarketplace(sourceRoot, targetRoot) {
  const sourceManifest = path.join(sourceRoot, ".agents", "plugins", "marketplace.json");
  const sourcePlugin = path.join(sourceRoot, "plugins", CODEX_PLUGIN_NAME);
  const targetManifest = path.join(targetRoot, ".agents", "plugins", "marketplace.json");
  const targetPlugin = path.join(targetRoot, "plugins", CODEX_PLUGIN_NAME);

  requireSource(sourceManifest, "file", "marketplace 清单");
  requireSource(sourcePlugin, "directory", "插件目录");

  // 整体替换插件副本：Codex 优先读取根目录 plugin.json，旧版残留会让它继续跳过插件钩子
  fs.rmSync(targetPlugin, { recursive: true, force: true });
  fs.mkdirSync(path.dirname(targetPlugin), { recursive: true });
  fs.cpSync(sourcePlugin, targetPlugin, {
    recursive: true,
    force: true,
    errorOnExist: false,
  });
  fs.mkdirSync(path.dirname(targetManifest), { recursive: true });
  fs.copyFileSync(sourceManifest, targetManifest);
}

function copyClaudeMarketplace(sourceRoot, targetRoot) {
  const sourceMarketplace = path.join(sourceRoot, "claude-marketplace");
  const sourceManifest = path.join(
    sourceMarketplace,
    ".claude-plugin",
    "marketplace.json",
  );
  const sourcePlugin = path.join(
    sourceMarketplace,
    "plugins",
    CLAUDE_PLUGIN_NAME,
  );

  requireSource(sourceMarketplace, "directory", "Claude marketplace 目录");
  requireSource(sourceManifest, "file", "Claude marketplace 清单");
  requireSource(sourcePlugin, "directory", "Claude 插件目录");
  fs.mkdirSync(path.dirname(targetRoot), { recursive: true });
  fs.cpSync(sourceMarketplace, targetRoot, {
    recursive: true,
    force: true,
    errorOnExist: false,
  });
}

function readClaudeSettings(settingsFile) {
  if (!fs.existsSync(settingsFile)) {
    return null;
  }
  let text;
  try {
    text = fs.readFileSync(settingsFile, "utf8");
  } catch (error) {
    throw new Error(`无法读取 Claude 配置 ${settingsFile}：${error.message}`);
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${settingsFile} 不是合法 JSON，未安装 Claude 插件：${error.message}`);
  }
}

function readCodexHooks(hooksFile) {
  if (!fs.existsSync(hooksFile)) {
    return null;
  }
  let text;
  try {
    text = fs.readFileSync(hooksFile, "utf8");
  } catch (error) {
    throw new Error(`无法读取 Codex 钩子配置 ${hooksFile}：${error.message}`);
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(`${hooksFile} 不是合法 JSON，未安装 Codex 插件：${error.message}`);
  }
}

function hasMatchingHookCommand(value, matches) {
  if (Array.isArray(value)) {
    return value.some((item) => hasMatchingHookCommand(item, matches));
  }
  if (!value || typeof value !== "object") {
    return false;
  }
  if (typeof value.command === "string" && matches(value.command)) {
    return true;
  }
  return Object.values(value).some((item) => hasMatchingHookCommand(item, matches));
}

function referencesLegacySessionTitleHook(value, legacyScript) {
  return hasMatchingHookCommand(
    value,
    (command) => {
      const expanded = command
        .replace(/\$\{CLAUDE_CONFIG_DIR\}|\$CLAUDE_CONFIG_DIR/g, path.dirname(path.dirname(legacyScript)))
        .replace(/\$\{HOME\}|\$HOME/g, os.homedir())
        .replace(/(^|[\s"'=])~(?=\/)/g, `$1${os.homedir()}`);
      return expanded.includes(legacyScript);
    },
  );
}

function referencesCodexLegacyHook(value, legacyScript, childEnv) {
  return hasMatchingHookCommand(value, (command) => {
    const expanded = command
      .replace(/\$\{CODEX_HOME\}|\$CODEX_HOME/g, childEnv.CODEX_HOME)
      .replace(/\$\{HOME\}|\$HOME/g, os.homedir())
      .replace(/(^|[\s"'=])~(?=\/)/g, `$1${os.homedir()}`);
    return expanded.includes(legacyScript);
  });
}

function languageArgs(options) {
  return options.language === null ? [] : ["--lang", options.language];
}

function isStandaloneLegacyCommand(command, scriptPath) {
  const expanded = command
    .replace(/\$\{CODEX_HOME\}|\$CODEX_HOME/g, path.dirname(path.dirname(scriptPath)))
    .replace(/\$\{CLAUDE_CONFIG_DIR\}|\$CLAUDE_CONFIG_DIR/g, path.dirname(path.dirname(scriptPath)))
    .replace(/\$\{HOME\}|\$HOME/g, os.homedir())
    .replace(/(^|[\s"'=])~(?=\/)/g, `$1${os.homedir()}`);
  const escaped = scriptPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`^\\s*(?:(?:/bin/)?(?:bash|sh)\\s+)?["']?${escaped}["']?\\s*$`).test(expanded);
}

// 旧版手工配置的桌面端即时改名钩子；插件已内置同一钩子，迁移时一并移除以免重复改名。
function isLegacyInstantTitleHook(hook) {
  return Boolean(
    hook
      && hook.type === "mcp_tool"
      && hook.server === "ccd_session_mgmt"
      && hook.tool === "set_session_title"
      && typeof hook.if === "string"
      && hook.if.includes(".cc-session-titles"),
  );
}

function migrateLegacyHooks(settingsFile, settings, detects, matches, extraLegacy = () => false) {
  const hooks = settings.hooks || settings;
  if (!hooks || typeof hooks !== "object" || Array.isArray(hooks)) {
    throw new Error(`${settingsFile} 的 hooks 结构无效，未迁移。`);
  }
  let removed = 0;
  for (const [event, entries] of Object.entries(hooks)) {
    if (!Array.isArray(entries)) {
      continue;
    }
    const retained = [];
    for (const entry of entries) {
      if (!entry || typeof entry !== "object" || !Array.isArray(entry.hooks)) {
        retained.push(entry);
        continue;
      }
      const remainingCommands = entry.hooks.filter((hook) => {
        const found = hook && typeof hook.command === "string" && detects(hook.command);
        if (found && !matches(hook.command)) {
          throw new Error(`${settingsFile} 含有复合或非标准的旧标题钩子命令，未迁移。`);
        }
        const legacy = (found && matches(hook.command)) || extraLegacy(hook);
        if (legacy) removed += 1;
        return !legacy;
      });
      if (remainingCommands.length > 0) {
        retained.push({ ...entry, hooks: remainingCommands });
      }
    }
    if (retained.length > 0) {
      hooks[event] = retained;
    } else {
      delete hooks[event];
    }
  }
  if (removed === 0) {
    throw new Error(`${settingsFile} 没有找到可安全迁移的旧钩子条目，未作修改。`);
  }
  const backup = `${settingsFile}.bak.session-titles.${Date.now()}`;
  const temporary = `${settingsFile}.session-titles.${process.pid}.tmp`;
  const mode = fs.statSync(settingsFile).mode & 0o777;
  const migratedText = `${JSON.stringify(settings, null, 2)}\n`;
  try {
    fs.copyFileSync(settingsFile, backup, fs.constants.COPYFILE_EXCL);
    fs.writeFileSync(temporary, migratedText, { mode, flag: "wx" });
    fs.renameSync(temporary, settingsFile);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch { /* 不存在时无需清理 */ }
    throw new Error(`迁移 ${settingsFile} 失败：${error.message}`);
  }
  note(`已备份 ${displayPath(settingsFile)} 至 ${displayPath(backup)}，移除 ${removed} 条旧版标题钩子`);
  return { backup, migratedText, mode };
}

function restoreLegacyHooks(settingsFile, migration) {
  const temporary = `${settingsFile}.session-titles-restore.${process.pid}.tmp`;
  try {
    if (fs.readFileSync(settingsFile, "utf8") !== migration.migratedText) {
      throw new Error("配置已被其他进程改动，不能自动恢复");
    }
    fs.copyFileSync(migration.backup, temporary, fs.constants.COPYFILE_EXCL);
    fs.chmodSync(temporary, migration.mode);
    fs.renameSync(temporary, settingsFile);
    note(`插件安装前置步骤失败，已从 ${displayPath(migration.backup)} 恢复旧钩子注册`);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch { /* 不存在时无需清理 */ }
    throw new Error(`无法自动恢复 ${settingsFile}，请核对备份 ${migration.backup}：${error.message}`);
  }
}

function renderRulesBlock(language) {
  return [RULES_START, ...RULES_TEXT[language], RULES_END].join("\n");
}

// 拆出托管块；标记缺失、重复或顺序错误时拒绝处理，避免误删用户内容
function splitManagedRules(text, rulesFile) {
  const starts = text.split(RULES_START).length - 1;
  const ends = text.split(RULES_END).length - 1;
  if (starts === 0 && ends === 0) {
    return null;
  }
  const startIndex = text.indexOf(RULES_START);
  const endIndex = text.indexOf(RULES_END);
  if (starts !== 1 || ends !== 1 || endIndex < startIndex) {
    throw new Error(`${rulesFile} 中的 agent-session-titles 规则标记不完整或重复，未作修改。`);
  }
  return {
    before: text.slice(0, startIndex),
    after: text.slice(endIndex + RULES_END.length),
  };
}

function joinRuleSections(before, block, after) {
  const parts = [before.replace(/\s+$/, ""), block, after.replace(/^\s+/, "").replace(/\s+$/, "")]
    .filter(Boolean);
  return parts.length > 0 ? `${parts.join("\n\n")}\n` : "";
}

// 返回托管块之外疑似手写标题规则的行号（从 1 开始）
function findTitleRuleConflicts(text, rulesFile) {
  const managed = splitManagedRules(text, rulesFile);
  // 托管块只保留换行，行号不变且不参与匹配
  const scanned = managed
    ? managed.before
      + text.slice(managed.before.length, text.length - managed.after.length).replace(/[^\n]/g, "")
      + managed.after
    : text;
  const conflicts = [];
  scanned.split("\n").forEach((line, index) => {
    if (TITLE_RULE_PATTERNS.some((pattern) => pattern.test(line))) conflicts.push(index + 1);
  });
  return conflicts;
}

// mode：write 写入或更新；refresh 仅在已有托管块时更新；remove 删除托管块
function updateRulesFile(rulesFile, language, mode) {
  const exists = fs.existsSync(rulesFile);
  const original = exists ? fs.readFileSync(rulesFile, "utf8") : "";
  const managed = splitManagedRules(original, rulesFile);
  let updated = original;
  if (mode === "remove") {
    if (!managed) return { changed: false, managed: false };
    updated = joinRuleSections(managed.before, "", managed.after);
  } else if (managed) {
    updated = joinRuleSections(managed.before, renderRulesBlock(language), managed.after);
  } else if (mode === "write") {
    updated = joinRuleSections(original, renderRulesBlock(language), "");
  } else {
    return { changed: false, managed: false };
  }
  if (updated === original) {
    return { changed: false, managed: mode !== "remove" };
  }
  let backup = null;
  const temporary = `${rulesFile}.session-titles.${process.pid}.tmp`;
  try {
    fs.mkdirSync(path.dirname(rulesFile), { recursive: true });
    const fileMode = exists ? fs.statSync(rulesFile).mode & 0o777 : 0o644;
    if (exists) {
      // 同一毫秒内连续更新时追加序号，避免备份名冲突
      const stamp = Date.now();
      for (let index = 0; backup === null || fs.existsSync(backup); index += 1) {
        backup = `${rulesFile}.bak.session-titles.${stamp}${index ? `-${index}` : ""}`;
      }
      fs.copyFileSync(rulesFile, backup, fs.constants.COPYFILE_EXCL);
    }
    fs.writeFileSync(temporary, updated, { mode: fileMode, flag: "wx" });
    fs.renameSync(temporary, rulesFile);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch { /* 不存在时无需清理 */ }
    throw new Error(`更新 ${rulesFile} 失败：${error.message}`);
  }
  return { changed: true, managed: mode !== "remove", backup };
}

function readSavedLanguage(configFile, fallback) {
  try {
    const language = JSON.parse(fs.readFileSync(configFile, "utf8")).language;
    if (["zh", "en"].includes(language)) return language;
  } catch { /* 读取失败时使用回退语言 */ }
  return fallback || "zh";
}

// 安装后处理全局规则文件：默认只提示疑似冲突；--rules 写入托管块；已有托管块随重装更新
function syncGlobalRules(rulesFile, languageFile, options) {
  return step("检查全局规则", () => {
    const language = readSavedLanguage(languageFile, options.language);
    let result;
    try {
      result = updateRulesFile(rulesFile, language, options.rules ? "write" : "refresh");
    } catch (error) {
      return { hint: displayPath(rulesFile), warning: error.message };
    }
    if (result.changed) {
      note(`已${options.rules ? "写入" : "更新"}标题规则块：${displayPath(rulesFile)}`);
      if (result.backup) note(`原文件已备份至 ${displayPath(result.backup)}`);
    }
    const text = fs.existsSync(rulesFile) ? fs.readFileSync(rulesFile, "utf8") : "";
    const conflicts = findTitleRuleConflicts(text, rulesFile);
    if (conflicts.length > 0) {
      return {
        hint: displayPath(rulesFile),
        warning: `第 ${conflicts.join("、")} 行疑似手写的会话标题规则，可能与插件注入的规则重复或冲突；`
          + "插件已自带规则，建议核对后删除或改写这些行。",
      };
    }
    return { hint: displayPath(rulesFile) };
  });
}

function removeGlobalRules(options) {
  const codexHome = resolveConfigDirectory(process.env.CODEX_HOME, path.join(os.homedir(), ".codex"));
  const claudeConfigDir = resolveConfigDirectory(
    process.env.CLAUDE_CONFIG_DIR,
    path.join(os.homedir(), ".claude"),
  );
  const files = [];
  if (options.target !== "claude") files.push(path.join(codexHome, "AGENTS.md"));
  if (options.target !== "codex") files.push(path.join(claudeConfigDir, "CLAUDE.md"));
  section("删除标题规则块");
  for (const rulesFile of files) {
    step(displayPath(rulesFile), () => {
      const result = updateRulesFile(rulesFile, "zh", "remove");
      if (result.backup) note(`原文件已备份至 ${displayPath(result.backup)}`);
      return { hint: result.changed ? "已删除" : "无托管规则块" };
    });
  }
  console.log("");
}

function outputLines(text) {
  return String(text || "").split(/\r?\n/).map((line) => line.trimEnd()).filter(Boolean);
}

// 子命令输出默认收起，只在失败时随错误展示；返回标准输出的各行
function runCommand(command, args, env, description) {
  const result = spawnSync(command, args, {
    env,
    stdio: ["inherit", "pipe", "pipe"],
    encoding: "utf8",
    shell: false,
  });
  let failure = null;
  if (result.error) {
    failure = `${description}失败：${result.error.message}`;
  } else if (result.signal) {
    failure = `${description}被信号 ${result.signal} 中止。`;
  } else if (result.status !== 0) {
    failure = `${description}失败，退出码 ${result.status}。`;
  }
  if (failure) {
    const error = new Error(failure);
    error.output = [...outputLines(result.stdout), ...outputLines(result.stderr)];
    throw error;
  }
  return outputLines(result.stdout);
}

function runSetupScript(script, options, env, description) {
  for (const line of runCommand("python3", [script, ...languageArgs(options)], env, description)) {
    const home = os.homedir();
    note(line.split(home).join("~").replace(/[。.]$/, ""));
  }
}

function tryRunCommand(command, args, env) {
  try {
    runCommand(command, args, env, command);
    return true;
  } catch {
    return false;
  }
}

function findCodexMarketplaceRoot(name, env) {
  let lines;
  try {
    lines = runCommand("codex", ["plugin", "marketplace", "list"], env, "读取 Codex 插件源列表");
  } catch {
    return null;
  }
  for (const line of lines) {
    const [listed, ...rest] = line.trim().split(/\s+/);
    if (listed === name) return rest.join(" ") || "";
  }
  return null;
}

function findClaudeMarketplace(name, env) {
  let lines;
  try {
    lines = runCommand("claude", ["plugin", "marketplace", "list", "--json"], env, "读取 Claude 插件源列表");
  } catch {
    return null;
  }
  try {
    const listed = JSON.parse(lines.join("\n"));
    return Array.isArray(listed) ? listed.find((item) => item && item.name === name) || null : null;
  } catch {
    return null;
  }
}

// 只删除安装器自己复制的旧目录，且目录内确有对应清单
function removeCopiedMarketplace(directory, manifestRelative) {
  if (!fs.existsSync(path.join(directory, manifestRelative))) return;
  fs.rmSync(directory, { recursive: true, force: true });
  note(`已删除旧插件源目录 ${displayPath(directory)}`);
}

function cleanupLegacyCodex(codexHome, env) {
  const legacy = LEGACY_CODEX_MARKETPLACE;
  const legacyRoot = path.join(codexHome, "marketplaces", legacy.directory);
  if (findCodexMarketplaceRoot(legacy.name, env) === null) {
    removeCopiedMarketplace(legacyRoot, path.join(".agents", "plugins", "marketplace.json"));
    return null;
  }
  return step("清理旧插件源", () => {
    if (tryRunCommand("codex", ["plugin", "remove", `${CODEX_PLUGIN_NAME}@${legacy.name}`], env)) {
      note(`已卸载 ${CODEX_PLUGIN_NAME}@${legacy.name}`);
    }
    if (!tryRunCommand("codex", ["plugin", "marketplace", "remove", legacy.name], env)) {
      return {
        hint: legacy.name,
        warning: `未能移除旧插件源，请手动运行：codex plugin marketplace remove ${legacy.name}`,
      };
    }
    removeCopiedMarketplace(legacyRoot, path.join(".agents", "plugins", "marketplace.json"));
    return { hint: legacy.name };
  });
}

function cleanupLegacyClaude(claudeConfigDir, env) {
  const legacy = LEGACY_CLAUDE_MARKETPLACE;
  const legacyRoot = path.join(claudeConfigDir, "marketplaces", legacy.directory);
  const manifest = path.join(".claude-plugin", "marketplace.json");
  if (!findClaudeMarketplace(legacy.name, env)) {
    removeCopiedMarketplace(legacyRoot, manifest);
    return null;
  }
  return step("清理旧插件源", () => {
    const legacyId = `${CLAUDE_PLUGIN_NAME}@${legacy.name}`;
    if (tryRunCommand("claude", ["plugin", "uninstall", legacyId, "--scope", "user"], env)) {
      note(`已卸载 ${legacyId}`);
    }
    if (!tryRunCommand("claude", ["plugin", "marketplace", "remove", legacy.name], env)) {
      return {
        hint: legacy.name,
        warning: `未能移除旧插件源，请手动运行：claude plugin marketplace remove ${legacy.name}`,
      };
    }
    removeCopiedMarketplace(legacyRoot, manifest);
    return { hint: legacy.name };
  });
}

function installCodex(sourceRoot, targetRoot, codexHome, childEnv, options) {
  const hooksFile = path.join(codexHome, "hooks.json");
  const legacyScript = path.join(codexHome, "hooks", "session-title.sh");
  const sourceSetupScript = path.join(
    sourceRoot,
    "plugins",
    CODEX_PLUGIN_NAME,
    "scripts",
    "setup.py",
  );
  const setupScript = path.join(
    targetRoot,
    "plugins",
    CODEX_PLUGIN_NAME,
    "scripts",
    "setup.py",
  );
  const pluginId = `${CODEX_PLUGIN_NAME}@${CODEX_MARKETPLACE_NAME}`;

  section("Codex");
  requireSource(sourceSetupScript, "file", "插件设置脚本");
  const hooks = readCodexHooks(hooksFile);
  let migration = null;
  if (hooks && referencesCodexLegacyHook(hooks, legacyScript, childEnv)) {
    if (!options.migrate) {
      throw new Error(`检测到 ${hooksFile} 的旧版标题钩子；请加 --migrate 备份并迁移。`);
    }
    step("迁移旧版钩子", () => {
      migration = migrateLegacyHooks(
        hooksFile, hooks,
        (command) => referencesCodexLegacyHook({ command }, legacyScript, childEnv),
        (command) => isStandaloneLegacyCommand(command, legacyScript),
      );
    });
  }

  let installAttempted = false;
  try {
    step("同步插件文件", () => {
      copyCodexMarketplace(sourceRoot, targetRoot);
      return { hint: displayPath(targetRoot) };
    });
    step("准备本地配置", () => {
      runSetupScript(setupScript, options, childEnv, "运行插件设置脚本");
    });
    step("注册插件源", () => {
      runCommand("codex", ["plugin", "marketplace", "add", targetRoot], childEnv, "添加 Codex 插件源");
      return { hint: CODEX_MARKETPLACE_NAME };
    });
    installAttempted = true;
    step("安装插件", () => {
      runCommand("codex", ["plugin", "add", pluginId], childEnv, "安装 Codex 会话标题插件");
      return { hint: pluginId };
    });
  } catch (error) {
    if (migration && !installAttempted) {
      try {
        restoreLegacyHooks(hooksFile, migration);
      } catch (restoreError) {
        throw new Error(`${error.message}；${restoreError.message}`);
      }
    } else if (migration) {
      note(`Codex 插件安装结果未确认；旧钩子配置备份：${displayPath(migration.backup)}`);
    }
    throw error;
  }

  cleanupLegacyCodex(codexHome, childEnv);
  syncGlobalRules(
    path.join(codexHome, "AGENTS.md"),
    path.join(codexHome, "session-titles", "config.json"),
    options,
  );
  return "重启 Codex 桌面端，在提示时检查并信任插件钩子";
}

// 旧版把 Claude 插件源复制在 Codex 目录下；重新注册到新位置后移除这份副本
function removeLegacyClaudeMarketplace(legacyRoot, marketplaceRoot) {
  const legacyManifest = path.join(legacyRoot, ".claude-plugin", "marketplace.json");
  if (path.resolve(legacyRoot) === path.resolve(marketplaceRoot) || !fs.existsSync(legacyManifest)) {
    return;
  }
  try {
    fs.rmSync(legacyRoot, { recursive: true, force: true });
    note(`已移除旧版 Claude 插件源副本 ${displayPath(legacyRoot)}`);
  } catch (error) {
    note(`未能移除旧版 Claude 插件源副本 ${displayPath(legacyRoot)}，可手动删除：${error.message}`);
  }
}

function installClaude(sourceRoot, claudeConfigDir, legacyMarketplaceRoot, childEnv, options) {
  const marketplaceRoot = path.join(claudeConfigDir, "marketplaces", CLAUDE_MARKETPLACE_NAME);
  const setupScript = path.join(sourceRoot, "scripts", "setup-claude.py");
  const settingsFile = path.join(claudeConfigDir, "settings.json");
  const legacyScript = path.join(claudeConfigDir, "hooks", "session-title.sh");
  const pluginId = `${CLAUDE_PLUGIN_NAME}@${CLAUDE_MARKETPLACE_NAME}`;

  section("Claude Code");
  requireSource(setupScript, "file", "Claude 设置脚本");
  const settings = readClaudeSettings(settingsFile);
  let migration = null;
  const hasLegacy = settings && referencesLegacySessionTitleHook(settings.hooks, legacyScript);
  if (hasLegacy) {
    if (!options.migrate) {
      throw new Error(`检测到 ${settingsFile} 的旧版标题钩子；请加 --migrate 备份并迁移。`);
    }
  }

  let installAttempted = false;
  try {
    step("同步插件文件", () => {
      copyClaudeMarketplace(sourceRoot, marketplaceRoot);
      return { hint: displayPath(marketplaceRoot) };
    });
    step("准备用户配置", () => {
      runSetupScript(setupScript, options, childEnv, "运行 Claude 设置脚本");
    });
    step("注册插件源", () => {
      runCommand(
        "claude",
        ["plugin", "marketplace", "add", marketplaceRoot, "--scope", "user"],
        childEnv,
        "添加 Claude 插件源",
      );
      return { hint: CLAUDE_MARKETPLACE_NAME };
    });
    if (hasLegacy) {
      step("迁移旧版钩子", () => {
        migration = migrateLegacyHooks(
          settingsFile, readClaudeSettings(settingsFile),
          (command) => referencesLegacySessionTitleHook({ command }, legacyScript),
          (command) => isStandaloneLegacyCommand(command, legacyScript),
          isLegacyInstantTitleHook,
        );
      });
    }

    installAttempted = true;
    step("安装插件", () => {
      runCommand(
        "claude",
        ["plugin", "install", pluginId, "--scope", "user"],
        childEnv,
        "安装 Claude 会话标题插件",
      );
      // 已安装时 install 不会刷新登记的版本号，升级需再执行 update
      runCommand(
        "claude",
        ["plugin", "update", pluginId, "--scope", "user"],
        childEnv,
        "更新 Claude 会话标题插件版本",
      );
      return { hint: pluginId };
    });
  } catch (error) {
    if (migration && !installAttempted) {
      try {
        restoreLegacyHooks(settingsFile, migration);
      } catch (restoreError) {
        throw new Error(`${error.message}；${restoreError.message}`);
      }
    } else if (migration) {
      note(`Claude 插件安装结果未确认；旧钩子配置备份：${displayPath(migration.backup)}`);
    }
    throw error;
  }

  cleanupLegacyClaude(claudeConfigDir, childEnv);
  removeLegacyClaudeMarketplace(legacyMarketplaceRoot, marketplaceRoot);
  const titleDir = process.env.CC_TITLE_DIR
    ? resolveConfigDirectory(process.env.CC_TITLE_DIR, "")
    : path.join(os.homedir(), ".cc-session-titles");
  syncGlobalRules(path.join(claudeConfigDir, "CLAUDE.md"), path.join(titleDir, "config.json"), options);
  return "重启 Claude Code 以加载插件";
}

function readVersion(sourceRoot) {
  try {
    return JSON.parse(fs.readFileSync(path.join(sourceRoot, "package.json"), "utf8")).version;
  } catch {
    return null;
  }
}

function printBanner(sourceRoot, options) {
  const version = readVersion(sourceRoot);
  const language = { zh: "中文", en: "English" }[options.language];
  console.log("");
  console.log(`  ${bold(cyan("Agent Session Titles"))}${version ? dim(`  v${version}`) : ""}`);
  console.log(`  ${dim(`会话标题插件安装器${language ? ` · 标题语言：${language}` : ""}`)}`);
}

function install(options) {
  const { target } = options;
  const nodeMajor = Number.parseInt(process.versions.node.split(".")[0], 10);
  if (!Number.isInteger(nodeMajor) || nodeMajor < 20) {
    throw new Error(`需要 Node.js 20 或更新版本，当前版本为 ${process.versions.node}。`);
  }

  const hasCodex = commandExists("codex");
  const hasClaude = commandExists("claude");
  const installCodexClient = target === "auto" ? hasCodex : target !== "claude";
  const installClaudeClient = target === "auto" ? hasClaude : target !== "codex";
  const missingClients = [];
  if (installCodexClient && !hasCodex) {
    missingClients.push("codex");
  }
  if (installClaudeClient && !hasClaude) {
    missingClients.push("claude");
  }
  if (missingClients.length > 0) {
    throw new Error(
      `所选安装目标在 PATH 中缺少命令：${missingClients.join("、")}。未作任何修改。`,
    );
  }
  if (target === "auto" && !hasCodex && !hasClaude) {
    throw new Error("PATH 中未检测到 codex 或 claude，至少需要安装其中一个客户端。");
  }

  const sourceRoot = path.resolve(__dirname, "..");
  const codexHome = resolveConfigDirectory(
    process.env.CODEX_HOME,
    path.join(os.homedir(), ".codex"),
  );
  const claudeConfigDir = resolveConfigDirectory(
    process.env.CLAUDE_CONFIG_DIR,
    path.join(os.homedir(), ".claude"),
  );
  const targetRoot = path.join(codexHome, "marketplaces", CODEX_MARKETPLACE_NAME);
  const childEnv = {
    ...process.env,
    CODEX_HOME: codexHome,
    CLAUDE_CONFIG_DIR: claudeConfigDir,
  };
  const failures = [];
  const nextSteps = [];

  printBanner(sourceRoot, options);

  if (installCodexClient) {
    try {
      nextSteps.push(installCodex(sourceRoot, targetRoot, codexHome, childEnv, options));
    } catch (error) {
      failures.push(`Codex：${error instanceof Error ? error.message : String(error)}`);
    }
  } else if (target === "auto") {
    section("Codex");
    printStepLine(dim("–"), dim("跳过：PATH 中未检测到 codex"));
  }

  if (installClaudeClient) {
    try {
      nextSteps.push(installClaude(
        sourceRoot,
        claudeConfigDir,
        path.join(codexHome, "marketplaces", LEGACY_CODEX_MARKETPLACE.directory, "claude-marketplace"),
        childEnv,
        options,
      ));
    } catch (error) {
      failures.push(`Claude：${error instanceof Error ? error.message : String(error)}`);
    }
  } else if (target === "auto") {
    section("Claude Code");
    printStepLine(dim("–"), dim("跳过：PATH 中未检测到 claude"));
  }

  if (failures.length > 0) {
    throw new Error(`安装未全部完成：\n- ${failures.join("\n- ")}`);
  }

  console.log(`\n  ${green(bold("安装完成"))}`);
  console.log(`  ${dim("下一步：")}`);
  nextSteps.forEach((text, index) => console.log(`  ${dim(`${index + 1}.`)} ${text}`));
  console.log("");
}

function main() {
  const args = process.argv.slice(2);
  if (args.length === 1 && (args[0] === "--help" || args[0] === "-h")) {
    printHelp();
    return;
  }
  const options = parseOptions(args);
  if (options.removeRules) {
    removeGlobalRules(options);
    return;
  }
  install(options);
}

// 仅在直接执行时安装，便于测试按模块引入迁移逻辑而不触发安装
if (require.main === module) {
  try {
    main();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`\n  ${red(bold("错误"))} ${message.split("\n").join("\n  ")}\n`);
    process.exitCode = 1;
  }
}

module.exports = {
  copyCodexMarketplace,
  findTitleRuleConflicts,
  parseOptions,
  renderRulesBlock,
  updateRulesFile,
  isLegacyInstantTitleHook,
  removeLegacyClaudeMarketplace,
  isStandaloneLegacyCommand,
  migrateLegacyHooks,
  referencesLegacySessionTitleHook,
};
