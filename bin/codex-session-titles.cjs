#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const CODEX_MARKETPLACE_NAME = "codex-session-titles-marketplace";
const CODEX_PLUGIN_NAME = "codex-session-titles";
const CLAUDE_MARKETPLACE_NAME = "session-titles";
const CLAUDE_PLUGIN_NAME = "claude-session-titles";

function printHelp() {
  console.log(`Codex 与 Claude Code 会话标题插件安装器

用法：
  npx --yes github:panda-z519/agent-session-titles
  npx --yes github:panda-z519/agent-session-titles --codex
  npx --yes github:panda-z519/agent-session-titles --claude
  npx --yes github:panda-z519/agent-session-titles --both
  npx --yes github:panda-z519/agent-session-titles --both --lang en

选项：
  --lang zh|en  标题语言；首次安装默认中文，更新时保留上次选择
  --migrate     备份配置并迁移旧版用户级标题钩子

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
    } else {
      throw new Error(`不支持的参数：${argument}。使用 --help 查看用法。`);
    }
  }
  return { target, language, migrate };
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
  console.log(`已备份 ${settingsFile} 至 ${backup}，并移除 ${removed} 条旧版标题钩子。`);
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
    console.log(`插件安装前置步骤失败，已从 ${migration.backup} 恢复旧钩子注册。`);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch { /* 不存在时无需清理 */ }
    throw new Error(`无法自动恢复 ${settingsFile}，请核对备份 ${migration.backup}：${error.message}`);
  }
}

function runCommand(command, args, env, description) {
  const result = spawnSync(command, args, {
    env,
    stdio: "inherit",
    shell: false,
  });
  if (result.error) {
    throw new Error(`${description}失败：${result.error.message}`);
  }
  if (result.signal) {
    throw new Error(`${description}被信号 ${result.signal} 中止。`);
  }
  if (result.status !== 0) {
    throw new Error(`${description}失败，退出码 ${result.status}。`);
  }
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

  requireSource(sourceSetupScript, "file", "插件设置脚本");
  const hooks = readCodexHooks(hooksFile);
  let migration = null;
  if (hooks && referencesCodexLegacyHook(hooks, legacyScript, childEnv)) {
    if (!options.migrate) {
      throw new Error(`检测到 ${hooksFile} 的旧版标题钩子；请加 --migrate 备份并迁移。`);
    }
    migration = migrateLegacyHooks(
      hooksFile, hooks,
      (command) => referencesCodexLegacyHook({ command }, legacyScript, childEnv),
      (command) => isStandaloneLegacyCommand(command, legacyScript),
    );
  }

  let installAttempted = false;
  try {
    console.log(`正在同步 Codex 插件文件到：${targetRoot}`);
    copyCodexMarketplace(sourceRoot, targetRoot);

    console.log("正在检查并准备 Codex 本地配置……");
    runCommand("python3", [setupScript, ...languageArgs(options)], childEnv, "运行插件设置脚本");

    console.log("正在添加 Codex 插件源……");
    runCommand(
      "codex",
      ["plugin", "marketplace", "add", targetRoot],
      childEnv,
      "添加 Codex 插件源",
    );

    console.log("正在安装 Codex 会话标题插件……");
    installAttempted = true;
    runCommand(
      "codex",
      ["plugin", "add", `${CODEX_PLUGIN_NAME}@${CODEX_MARKETPLACE_NAME}`],
      childEnv,
      "安装 Codex 会话标题插件",
    );
  } catch (error) {
    if (migration && !installAttempted) {
      try {
        restoreLegacyHooks(hooksFile, migration);
      } catch (restoreError) {
        throw new Error(`${error.message}；${restoreError.message}`);
      }
    } else if (migration) {
      console.error(`Codex 插件安装结果未确认；旧钩子配置备份：${migration.backup}`);
    }
    throw error;
  }

  console.log("Codex 插件安装完成。请重启 Codex 桌面端，并在提示时检查并信任插件钩子。");
}

function installClaude(sourceRoot, targetRoot, claudeConfigDir, childEnv, options) {
  const marketplaceRoot = path.join(targetRoot, "claude-marketplace");
  const setupScript = path.join(sourceRoot, "scripts", "setup-claude.py");
  const settingsFile = path.join(claudeConfigDir, "settings.json");
  const legacyScript = path.join(claudeConfigDir, "hooks", "session-title.sh");

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
    console.log(`正在同步 Claude 插件文件到：${marketplaceRoot}`);
    copyClaudeMarketplace(sourceRoot, marketplaceRoot);
    console.log("正在检查并准备 Claude Code 用户配置……");
    runCommand("python3", [setupScript, ...languageArgs(options)], childEnv, "运行 Claude 设置脚本");
    console.log("正在添加 Claude 插件源……");
    runCommand(
      "claude",
      ["plugin", "marketplace", "add", marketplaceRoot, "--scope", "user"],
      childEnv,
      "添加 Claude 插件源",
    );
    if (hasLegacy) {
      migration = migrateLegacyHooks(
        settingsFile, readClaudeSettings(settingsFile),
        (command) => referencesLegacySessionTitleHook({ command }, legacyScript),
        (command) => isStandaloneLegacyCommand(command, legacyScript),
        isLegacyInstantTitleHook,
      );
    }

    console.log("正在安装 Claude 会话标题插件……");
    installAttempted = true;
    runCommand(
      "claude",
      [
        "plugin",
        "install",
        `${CLAUDE_PLUGIN_NAME}@${CLAUDE_MARKETPLACE_NAME}`,
        "--scope",
        "user",
      ],
      childEnv,
      "安装 Claude 会话标题插件",
    );
  } catch (error) {
    if (migration && !installAttempted) {
      try {
        restoreLegacyHooks(settingsFile, migration);
      } catch (restoreError) {
        throw new Error(`${error.message}；${restoreError.message}`);
      }
    } else if (migration) {
      console.error(`Claude 插件安装结果未确认；旧钩子配置备份：${migration.backup}`);
    }
    throw error;
  }

  console.log("Claude 插件安装完成。请重启 Claude Code 以加载插件。");
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
  const targetRoot = path.join(codexHome, "marketplaces", "codex-session-titles");
  const childEnv = {
    ...process.env,
    CODEX_HOME: codexHome,
    CLAUDE_CONFIG_DIR: claudeConfigDir,
  };
  const failures = [];

  if (installCodexClient) {
    try {
      installCodex(sourceRoot, targetRoot, codexHome, childEnv, options);
    } catch (error) {
      failures.push(`Codex：${error instanceof Error ? error.message : String(error)}`);
    }
  } else if (target === "auto") {
    console.log("PATH 中未检测到 codex，跳过 Codex 插件安装。");
  }

  if (installClaudeClient) {
    try {
      installClaude(sourceRoot, targetRoot, claudeConfigDir, childEnv, options);
    } catch (error) {
      failures.push(`Claude：${error instanceof Error ? error.message : String(error)}`);
    }
  } else if (target === "auto") {
    console.log("PATH 中未检测到 claude，跳过 Claude 插件安装。");
  }

  if (failures.length > 0) {
    throw new Error(`安装未全部完成：\n- ${failures.join("\n- ")}`);
  }
}

function main() {
  const args = process.argv.slice(2);
  if (args.length === 1 && (args[0] === "--help" || args[0] === "-h")) {
    printHelp();
    return;
  }
  install(parseOptions(args));
}

// 仅在直接执行时安装，便于测试按模块引入迁移逻辑而不触发安装
if (require.main === module) {
  try {
    main();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`错误：${message}`);
    process.exitCode = 1;
  }
}

module.exports = {
  isLegacyInstantTitleHook,
  isStandaloneLegacyCommand,
  migrateLegacyHooks,
  referencesLegacySessionTitleHook,
};
