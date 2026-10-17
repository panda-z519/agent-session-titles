// 验证安装器迁移旧版 Claude 标题钩子：只移除标题相关条目，保留其他钩子并备份。
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  isLegacyInstantTitleHook,
  isStandaloneLegacyCommand,
  migrateLegacyHooks,
  referencesLegacySessionTitleHook,
  removeLegacyClaudeMarketplace,
} = require("../bin/codex-session-titles.cjs");

const legacyScript = path.join(os.homedir(), ".claude", "hooks", "session-title.sh");

function migrate(settings) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "session-titles-migration-"));
  const settingsFile = path.join(directory, "settings.json");
  fs.writeFileSync(settingsFile, JSON.stringify(settings));
  const result = migrateLegacyHooks(
    settingsFile,
    JSON.parse(fs.readFileSync(settingsFile, "utf8")),
    (command) => referencesLegacySessionTitleHook({ command }, legacyScript),
    (command) => isStandaloneLegacyCommand(command, legacyScript),
    isLegacyInstantTitleHook,
  );
  return { migrated: JSON.parse(fs.readFileSync(settingsFile, "utf8")), backup: result.backup };
}

const legacyCommand = { type: "command", command: 'bash "$HOME/.claude/hooks/session-title.sh"' };
const instantHook = {
  type: "mcp_tool",
  server: "ccd_session_mgmt",
  tool: "set_session_title",
  input: { session_id: "self", title: "${tool_input.content}" },
  if: "Write(~/.cc-session-titles/**)",
};

test("移除旧版命令钩子和即时改名钩子，保留无关钩子", () => {
  const unrelated = { type: "command", command: "echo keep" };
  const { migrated, backup } = migrate({
    model: "keep",
    hooks: {
      SessionStart: [{ hooks: [legacyCommand] }],
      UserPromptSubmit: [{ hooks: [legacyCommand, unrelated] }],
      PostToolUse: [{ matcher: "Write", hooks: [instantHook] }],
    },
  });

  assert.equal(migrated.model, "keep");
  assert.deepEqual(migrated.hooks, { UserPromptSubmit: [{ hooks: [unrelated] }] });
  assert.ok(fs.existsSync(backup));
});

test("不移除指向其他目录或工具的 mcp_tool 钩子", () => {
  const otherTool = { ...instantHook, tool: "rename_tab" };
  const otherPath = { ...instantHook, if: "Write(~/notes/**)" };
  const { migrated } = migrate({
    hooks: {
      SessionStart: [{ hooks: [legacyCommand] }],
      PostToolUse: [{ matcher: "Write", hooks: [otherTool, otherPath] }],
    },
  });

  assert.deepEqual(migrated.hooks.PostToolUse, [{ matcher: "Write", hooks: [otherTool, otherPath] }]);
});

test("只移除带 Claude 清单的旧插件源副本，不动其他目录", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "session-titles-legacy-"));
  const legacy = path.join(directory, "codex", "claude-marketplace");
  const current = path.join(directory, "claude", "marketplaces", "session-titles");
  const unrelated = path.join(directory, "codex", "other");
  fs.mkdirSync(path.join(legacy, ".claude-plugin"), { recursive: true });
  fs.writeFileSync(path.join(legacy, ".claude-plugin", "marketplace.json"), "{}");
  fs.mkdirSync(unrelated, { recursive: true });

  removeLegacyClaudeMarketplace(legacy, current);
  removeLegacyClaudeMarketplace(unrelated, current);
  removeLegacyClaudeMarketplace(current, current);

  assert.equal(fs.existsSync(legacy), false);
  assert.equal(fs.existsSync(unrelated), true);
});
