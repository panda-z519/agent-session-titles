// 验证全局规则托管块：默认不改文件，--rules 写入且可重复更新，--remove-rules 原样删除。
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  findTitleRuleConflicts,
  parseOptions,
  renderRulesBlock,
  updateRulesFile,
} = require("../bin/agent-session-titles.cjs");

function rulesFile(content) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "session-titles-rules-"));
  const file = path.join(directory, "AGENTS.md");
  if (content !== undefined) fs.writeFileSync(file, content);
  return file;
}

const userRules = "# 个人规则\n\n- 用中文回复。\n";

test("refresh 模式在没有托管块时不修改文件", () => {
  const file = rulesFile(userRules);
  const result = updateRulesFile(file, "zh", "refresh");
  assert.equal(result.changed, false);
  assert.equal(fs.readFileSync(file, "utf8"), userRules);
  assert.deepEqual(fs.readdirSync(path.dirname(file)), ["AGENTS.md"]);
});

test("write 追加托管块并备份，重复写入只替换不重复", () => {
  const file = rulesFile(userRules);
  const first = updateRulesFile(file, "zh", "write");
  assert.equal(first.changed, true);
  assert.equal(fs.readFileSync(first.backup, "utf8"), userRules);
  const written = fs.readFileSync(file, "utf8");
  assert.ok(written.startsWith(userRules.trimEnd()));
  assert.ok(written.includes(renderRulesBlock("zh")));

  const switched = updateRulesFile(file, "en", "refresh");
  assert.equal(switched.changed, true);
  const refreshed = fs.readFileSync(file, "utf8");
  assert.equal(refreshed.split("agent-session-titles:start").length - 1, 1);
  assert.ok(refreshed.includes("Session titles (agent-session-titles plugin)"));
  assert.ok(!refreshed.includes("会话标题（agent-session-titles 插件）"));

  assert.equal(updateRulesFile(file, "en", "write").changed, false, "内容相同不应再备份或改写");
});

test("remove 删除托管块并恢复用户原有内容", () => {
  const file = rulesFile(userRules);
  updateRulesFile(file, "zh", "write");
  fs.appendFileSync(file, "\n- 之后追加的个人规则。\n");
  const result = updateRulesFile(file, "zh", "remove");
  assert.equal(result.changed, true);
  assert.equal(
    fs.readFileSync(file, "utf8"),
    "# 个人规则\n\n- 用中文回复。\n\n- 之后追加的个人规则。\n",
  );
});

test("write 在规则文件不存在时创建文件", () => {
  const file = rulesFile();
  const result = updateRulesFile(file, "zh", "write");
  assert.equal(result.backup, null);
  assert.equal(fs.readFileSync(file, "utf8"), `${renderRulesBlock("zh")}\n`);
});

test("标记不完整时拒绝修改", () => {
  const broken = `${userRules}\n<!-- agent-session-titles:start -->\n手写内容\n`;
  const file = rulesFile(broken);
  assert.throws(() => updateRulesFile(file, "zh", "write"), /标记不完整或重复/);
  assert.equal(fs.readFileSync(file, "utf8"), broken);
});

test("冲突提示只指出托管块之外的手写标题规则", () => {
  const text = [
    "# 个人规则",
    "## 会话标题约定",
    "- 会话名采用「状态｜初始需求｜当前进展」。",
    "- 用中文回复。",
    "",
    renderRulesBlock("zh"),
    "- Keep the session title short.",
  ].join("\n");
  assert.deepEqual(findTitleRuleConflicts(text, "AGENTS.md"), [2, 3, 16]);
  assert.deepEqual(findTitleRuleConflicts(renderRulesBlock("en"), "AGENTS.md"), []);
});

test("--rules 与 --remove-rules 互斥，--remove-rules 不接受安装参数", () => {
  assert.equal(parseOptions(["--rules"]).rules, true);
  assert.equal(parseOptions(["--codex", "--remove-rules"]).removeRules, true);
  assert.throws(() => parseOptions(["--rules", "--remove-rules"]), /只能指定其中一个/);
  assert.throws(() => parseOptions(["--remove-rules", "--lang", "en"]), /不能与 --lang/);
});
