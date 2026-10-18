#!/usr/bin/env python3
"""在隔离目录中验证 Claude Code 会话标题钩子。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
HOOK = ROOT.parent / "hooks" / "session-title.py"


class SessionTitleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="claude-session-title-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.title_dir = self.root / "title-state"
        self.cwd = self.root / "workspace"
        self.home.mkdir()
        self.cwd.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def title_path(self, session_id: str) -> Path:
        return self.title_dir / f"{session_id}.txt"

    def applied_path(self, session_id: str) -> Path:
        return self.title_dir / f"{session_id}.applied"

    def write_title(self, session_id: str, title: str) -> None:
        self.title_dir.mkdir(parents=True, exist_ok=True)
        self.title_path(session_id).write_text(title + "\n", encoding="utf-8")

    def run_hook(
        self,
        event: str,
        session_id: str,
        *,
        source: str | None = None,
        session_title: str | None = None,
        cwd: Path | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "hook_event_name": event,
            "session_id": session_id,
            "cwd": str(cwd or self.cwd),
        }
        if source is not None:
            payload["source"] = source
        if session_title is not None:
            payload["session_title"] = session_title
        if prompt is not None:
            payload["prompt"] = prompt
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["CC_TITLE_DIR"] = str(self.title_dir)
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=self.cwd,
            env=env,
            check=True,
        )
        return json.loads(result.stdout) if result.stdout.strip() else None

    def hook_output(self, result: dict[str, Any]) -> dict[str, Any]:
        return result["hookSpecificOutput"]

    def state_path(self, session_id: str) -> Path:
        return self.title_dir / f"{session_id}.state.json"

    def state(self, session_id: str) -> dict[str, Any]:
        return json.loads(self.state_path(session_id).read_text(encoding="utf-8"))

    def test_new_session_start_returns_writing_guidance(self) -> None:
        session_id = "session-startup"

        output = self.hook_output(self.run_hook("SessionStart", session_id, source="startup"))

        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertNotIn("sessionTitle", output)
        self.assertIn(str(self.title_path(session_id)), output["additionalContext"])
        self.assertFalse(self.applied_path(session_id).exists())

    def test_resumed_session_without_baseline_protects_whole_name(self) -> None:
        session_id = "session-resume"
        self.write_title(session_id, "开发中｜会话标题插件｜等待首次提示词")

        output = self.hook_output(
            self.run_hook(
                "SessionStart",
                session_id,
                source="resume",
                session_title="已有会话名称",
            )
        )

        self.assertNotIn("sessionTitle", output)
        self.assertIn("整名保护", output["additionalContext"])
        self.assertTrue(self.state(session_id)["whole_name_protected"])

    def test_title_file_is_applied_then_confirmed(self) -> None:
        session_id = "valid-title"
        requested = "开发中｜会话标题插件｜实现钩子运行逻辑"
        self.write_title(session_id, requested)

        first = self.hook_output(self.run_hook("UserPromptSubmit", session_id))
        self.assertEqual(first["sessionTitle"], requested)
        # 会话名尚未回传前不写确认基线
        self.assertFalse(self.applied_path(session_id).exists())

        second = self.hook_output(
            self.run_hook("UserPromptSubmit", session_id, session_title=requested)
        )
        self.assertNotIn("sessionTitle", second)
        self.assertIn("标题仍准确时不要写", second["additionalContext"])
        self.assertEqual(
            self.applied_path(session_id).read_text(encoding="utf-8"),
            requested + "\n",
        )
        self.assertEqual(self.state(session_id)["locked_parts"], [False, True, False])

    def test_first_prompt_without_title_file_uses_prompt_summary(self) -> None:
        session_id = "missing-title"

        output = self.hook_output(
            self.run_hook("UserPromptSubmit", session_id, prompt="请帮我优化会话标题插件")
        )

        self.assertEqual(output["sessionTitle"], "摸底｜优化会话标题插件｜分析首条请求")
        self.assertIn(str(self.title_path(session_id)), output["additionalContext"])
        self.assertFalse(self.applied_path(session_id).exists())

    def test_english_mode_does_not_mix_cjk_prompt_into_title(self) -> None:
        self.title_dir.mkdir(parents=True)
        (self.title_dir / "config.json").write_text('{"language": "en"}', encoding="utf-8")

        cjk = self.hook_output(
            self.run_hook("UserPromptSubmit", "en-cjk", prompt="帮我修复登录页面的报错")
        )
        english = self.hook_output(
            self.run_hook("UserPromptSubmit", "en-ascii", prompt="Please fix the login page error")
        )

        self.assertEqual(
            cjk["sessionTitle"], "Scoping | Handle first request | Reviewing first request"
        )
        self.assertEqual(
            english["sessionTitle"], "Scoping | fix the login page error | Reviewing first request"
        )

    def test_sensitive_first_prompt_is_not_used_as_title(self) -> None:
        output = self.hook_output(
            self.run_hook("UserPromptSubmit", "sensitive", prompt="我的 api_key 是 sk-abcdefgh123")
        )

        self.assertEqual(output["sessionTitle"], "摸底｜处理首条请求｜分析首条请求")

    def test_projectless_session_does_not_add_directory_name(self) -> None:
        session_id = "projectless-title"
        requested = "摸底｜会话标题插件｜确认输入输出契约"
        self.write_title(session_id, requested)

        result = self.run_hook("UserPromptSubmit", session_id)

        self.assertEqual(self.hook_output(result)["sessionTitle"], requested)
        self.assertNotIn(self.cwd.name, self.hook_output(result)["sessionTitle"])

    def test_worktree_session_adds_prefix_once(self) -> None:
        repository = self.root / "repository"
        worktree = self.root / "worktree"
        repository.mkdir()
        git_env = os.environ.copy()
        git_env["HOME"] = str(self.home)
        git_env["XDG_CONFIG_HOME"] = str(self.root / "git-config")
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(repository)],
            env=git_env,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                "base",
            ],
            env=git_env,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "worktree",
                "add",
                "-q",
                "-b",
                "session-title-test",
                str(worktree),
                "main",
            ],
            env=git_env,
            check=True,
        )
        session_id = "worktree-title"
        self.write_title(
            session_id,
            "[工作树] 开发中｜会话标题插件｜验证工作树前缀",
        )

        result = self.run_hook("UserPromptSubmit", session_id, cwd=worktree)

        self.assertEqual(
            self.hook_output(result)["sessionTitle"],
            "[工作树] 开发中｜会话标题插件｜验证工作树前缀",
        )
        # 桌面端按文件内容原样改名，提示里必须要求 Agent 自带前缀
        self.assertIn(
            "「[工作树] 状态｜初始需求｜当前进展」",
            self.hook_output(result)["additionalContext"],
        )

    def establish_baseline(self, session_id: str, title: str) -> None:
        self.write_title(session_id, title)
        self.run_hook("UserPromptSubmit", session_id)
        self.run_hook("UserPromptSubmit", session_id, session_title=title)

    def test_unparseable_manual_rename_protects_whole_name(self) -> None:
        session_id = "external-rename"
        first_title = "开发中｜会话标题插件｜建立标题基线"
        self.establish_baseline(session_id, first_title)
        self.write_title(session_id, "测试｜会话标题插件｜保护外部改名")

        output = self.hook_output(
            self.run_hook("UserPromptSubmit", session_id, session_title="人工修改的名称")
        )

        self.assertNotIn("sessionTitle", output)
        self.assertIn("整名保护", output["additionalContext"])
        self.assertEqual(
            self.applied_path(session_id).read_text(encoding="utf-8"),
            first_title + "\n",
        )

    def test_manual_part_edit_is_locked_and_preserved(self) -> None:
        session_id = "manual-part"
        self.establish_baseline(session_id, "开发中｜会话标题插件｜建立标题基线")
        self.write_title(session_id, "测试｜会话标题插件｜跑通单元测试")

        output = self.hook_output(
            self.run_hook(
                "UserPromptSubmit",
                session_id,
                session_title="开发中｜会话标题插件｜我自己写的进展",
            )
        )

        self.assertEqual(output["sessionTitle"], "测试｜会话标题插件｜我自己写的进展")
        self.assertEqual(self.state(session_id)["locked_parts"], [False, True, True])

    def test_instantly_applied_title_is_not_treated_as_manual_edit(self) -> None:
        session_id = "instant-title"
        self.establish_baseline(session_id, "开发中｜会话标题插件｜建立标题基线")
        # 桌面端 PostToolUse 钩子在写入后立即把会话名改成文件内容
        instant = "测试｜会话标题插件｜跑通单元测试"
        self.write_title(session_id, instant)

        output = self.hook_output(
            self.run_hook("UserPromptSubmit", session_id, session_title=instant)
        )

        self.assertNotIn("sessionTitle", output)
        state = self.state(session_id)
        self.assertEqual(state["locked_parts"], [False, True, False])
        self.assertEqual(state["confirmed_parts"], instant.split("｜"))
        self.assertEqual(
            self.applied_path(session_id).read_text(encoding="utf-8"), instant + "\n"
        )

    def test_instant_title_restores_locked_part(self) -> None:
        session_id = "instant-locked"
        self.establish_baseline(session_id, "开发中｜会话标题插件｜建立标题基线")
        self.run_hook(
            "UserPromptSubmit",
            session_id,
            session_title="开发中｜会话标题插件｜我自己写的进展",
        )
        self.run_hook(
            "UserPromptSubmit",
            session_id,
            session_title="开发中｜会话标题插件｜我自己写的进展",
        )
        instant = "测试｜会话标题插件｜覆盖了锁定字段"
        self.write_title(session_id, instant)

        output = self.hook_output(
            self.run_hook("UserPromptSubmit", session_id, session_title=instant)
        )

        self.assertEqual(output["sessionTitle"], "测试｜会话标题插件｜我自己写的进展")

    def test_desktop_auto_name_before_confirmation_does_not_protect(self) -> None:
        session_id = "auto-name"
        self.run_hook("UserPromptSubmit", session_id, prompt="优化会话标题插件")
        # 桌面端首轮后自动起名，覆盖了尚未确认的插件标题
        output = self.hook_output(
            self.run_hook("UserPromptSubmit", session_id, session_title="今天状态不错")
        )

        self.assertEqual(output["sessionTitle"], "摸底｜优化会话标题插件｜分析首条请求")
        self.assertNotIn("整名保护", output["additionalContext"])
        self.assertIn(str(self.title_path(session_id)), output["additionalContext"])
        self.assertFalse(self.state(session_id)["whole_name_protected"])

    def test_instant_title_confirms_after_desktop_auto_name(self) -> None:
        session_id = "auto-name-instant"
        self.run_hook("UserPromptSubmit", session_id, prompt="优化会话标题插件")
        self.run_hook("UserPromptSubmit", session_id, session_title="Optimize title plugin")
        instant = "方案｜优化会话标题插件｜列出改进项"
        self.write_title(session_id, instant)

        output = self.hook_output(
            self.run_hook("UserPromptSubmit", session_id, session_title=instant)
        )

        self.assertNotIn("sessionTitle", output)
        state = self.state(session_id)
        self.assertFalse(state["whole_name_protected"])
        self.assertEqual(state["confirmed_parts"], instant.split("｜"))

    def test_protected_name_restored_to_three_parts_resumes_updates(self) -> None:
        session_id = "restore-parts"
        self.establish_baseline(session_id, "开发中｜会话标题插件｜建立标题基线")
        self.run_hook("UserPromptSubmit", session_id, session_title="人工修改的名称")
        restored = "测试｜会话标题插件｜手动恢复三段"

        output = self.hook_output(
            self.run_hook("UserPromptSubmit", session_id, session_title=restored)
        )

        self.assertNotIn("整名保护", output["additionalContext"])
        state = self.state(session_id)
        self.assertFalse(state["whole_name_protected"])
        self.assertEqual(state["confirmed_parts"], restored.split("｜"))
        self.assertEqual(state["locked_parts"], [False, True, False])
        self.write_title(session_id, "已完成｜会话标题插件｜恢复后继续更新")
        updated = self.hook_output(
            self.run_hook("UserPromptSubmit", session_id, session_title=restored)
        )
        self.assertEqual(updated["sessionTitle"], "已完成｜会话标题插件｜恢复后继续更新")

    def test_protected_name_without_baseline_stays_protected_when_parseable(self) -> None:
        session_id = "resume-parseable"
        self.run_hook(
            "SessionStart", session_id, source="resume", session_title="别处的名称"
        )

        output = self.hook_output(
            self.run_hook(
                "UserPromptSubmit", session_id, session_title="开发中｜旧会话｜来源未确认"
            )
        )

        self.assertIn("整名保护", output["additionalContext"])
        self.assertTrue(self.state(session_id)["whole_name_protected"])

    def test_invalid_titles_are_not_applied(self) -> None:
        invalid_titles = (
            "处理中｜会话标题插件｜使用白名单外状态",
            "开发中｜缺少当前进展",
            "开发中｜｜当前进展为空",
        )
        for index, requested in enumerate(invalid_titles):
            with self.subTest(requested=requested):
                session_id = f"invalid-title-{index}"
                self.write_title(session_id, requested)

                result = self.run_hook("UserPromptSubmit", session_id)

                output = self.hook_output(result)
                self.assertNotIn("sessionTitle", output)
                self.assertIn("内容无效", output["additionalContext"])
                self.assertFalse(self.applied_path(session_id).exists())

    def test_invalid_session_id_cannot_escape_title_directory(self) -> None:
        escaped = self.root / "escaped.applied"
        result = self.run_hook("UserPromptSubmit", "../escaped")

        self.assertIsNone(result)
        self.assertFalse(escaped.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
