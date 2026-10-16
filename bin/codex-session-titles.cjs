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
  npx --yes --package=github:panda-z519/codex-session-titles codex-session-titles
  npx --yes --package=github:panda-z519/codex-session-titles codex-session-titles --help

环境变量：
  CODEX_HOME  Codex 配置目录，默认使用 ~/.codex
  CLAUDE_CONFIG_DIR  Claude Code 配置目录，默认使用 ~/.claude

安装器会检测 PATH 中的 codex 和 claude，至少安装一个可用客户端的插件。
若 Codex 或 Claude Code 已配置旧版用户级 session-title.sh 钩子，将保留旧钩子
并跳过对应插件，避免同一事件重复执行。`);
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

function referencesLegacySessionTitleHook(value) {
  return hasMatchingHookCommand(
    value,
    (command) => command.includes("session-title.sh"),
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

function installCodex(sourceRoot, targetRoot, codexHome, childEnv) {
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

  const hooks = readCodexHooks(hooksFile);
  if (hooks && referencesCodexLegacyHook(hooks, legacyScript, childEnv)) {
    console.log(
      `检测到 ${hooksFile} 中已有指向 ${legacyScript} 的用户级钩子；`
      + "已保留该钩子并跳过 Codex 插件安装，避免同一事件重复执行。",
    );
    return;
  }

  requireSource(sourceSetupScript, "file", "插件设置脚本");
  console.log(`正在同步 Codex 插件文件到：${targetRoot}`);
  copyCodexMarketplace(sourceRoot, targetRoot);

  console.log("正在检查并准备 Codex 本地配置……");
  runCommand("python3", [setupScript], childEnv, "运行插件设置脚本");

  console.log("正在添加 Codex 插件源……");
  runCommand(
    "codex",
    ["plugin", "marketplace", "add", targetRoot],
    childEnv,
    "添加 Codex 插件源",
  );

  console.log("正在安装 Codex 会话标题插件……");
  runCommand(
    "codex",
    ["plugin", "add", `${CODEX_PLUGIN_NAME}@${CODEX_MARKETPLACE_NAME}`],
    childEnv,
    "安装 Codex 会话标题插件",
  );

  console.log("Codex 插件安装完成。请重启 Codex 桌面端，并在提示时检查并信任插件钩子。");
}

function installClaude(sourceRoot, targetRoot, claudeConfigDir, childEnv) {
  const marketplaceRoot = path.join(targetRoot, "claude-marketplace");
  const setupScript = path.join(sourceRoot, "scripts", "setup-claude.py");
  const settingsFile = path.join(claudeConfigDir, "settings.json");

  requireSource(setupScript, "file", "Claude 设置脚本");
  console.log(`正在同步 Claude 插件文件到：${marketplaceRoot}`);
  copyClaudeMarketplace(sourceRoot, marketplaceRoot);

  const settings = readClaudeSettings(settingsFile);
  if (settings && referencesLegacySessionTitleHook(settings.hooks)) {
    console.log(
      `检测到 ${settingsFile} 中已有引用 session-title.sh 的用户级钩子；`
      + "已保留该钩子并跳过 Claude 插件安装，避免同一事件重复执行。",
    );
    return;
  }

  console.log("正在检查并准备 Claude Code 用户配置……");
  runCommand("python3", [setupScript], childEnv, "运行 Claude 设置脚本");

  console.log("正在添加 Claude 插件源……");
  runCommand(
    "claude",
    ["plugin", "marketplace", "add", marketplaceRoot, "--scope", "user"],
    childEnv,
    "添加 Claude 插件源",
  );

  console.log("正在安装 Claude 会话标题插件……");
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

  console.log("Claude 插件安装完成。请重启 Claude Code 以加载插件。");
}

function install() {
  const nodeMajor = Number.parseInt(process.versions.node.split(".")[0], 10);
  if (!Number.isInteger(nodeMajor) || nodeMajor < 20) {
    throw new Error(`需要 Node.js 20 或更新版本，当前版本为 ${process.versions.node}。`);
  }

  const hasCodex = commandExists("codex");
  const hasClaude = commandExists("claude");
  if (!hasCodex && !hasClaude) {
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

  if (hasCodex) {
    try {
      installCodex(sourceRoot, targetRoot, codexHome, childEnv);
    } catch (error) {
      failures.push(`Codex：${error instanceof Error ? error.message : String(error)}`);
    }
  } else {
    console.log("PATH 中未检测到 codex，跳过 Codex 插件安装。");
  }

  if (hasClaude) {
    try {
      installClaude(sourceRoot, targetRoot, claudeConfigDir, childEnv);
    } catch (error) {
      failures.push(`Claude：${error instanceof Error ? error.message : String(error)}`);
    }
  } else {
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
  if (args.length > 0) {
    throw new Error(`不支持的参数：${args.join(" ")}。使用 --help 查看用法。`);
  }
  install();
}

try {
  main();
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  console.error(`错误：${message}`);
  process.exitCode = 1;
}
