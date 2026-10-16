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

    def test_session_start_preserves_startup_and_resumed_names(self) -> None:
        sessions = (("startup", "Claude 自动标题"), ("resume", "已有会话名称"))
        for source, current in sessions:
            with self.subTest(source=source):
                session_id = f"session-{source}"
                self.write_title(
                    session_id,
                    "开发中｜会话标题插件｜等待首次提示词",
                )
                result = self.run_hook(
                    "SessionStart",
                    session_id,
                    source=source,
                    session_title=current,
                )
                self.assertIsNotNone(result)
                output = self.hook_output(result)
                self.assertEqual(output["hookEventName"], "SessionStart")
                self.assertNotIn("sessionTitle", output)
                self.assertIn(str(self.title_path(session_id)), output["additionalContext"])
                self.assertFalse(self.applied_path(session_id).exists())

    def test_valid_three_part_title_is_applied_and_recorded(self) -> None:
        session_id = "valid-title"
        requested = "开发中｜会话标题插件｜实现钩子运行逻辑"
        self.write_title(session_id, requested)

        result = self.run_hook(
            "UserPromptSubmit",
            session_id,
            session_title="Claude 自动标题",
        )

        self.assertIsNotNone(result)
        output = self.hook_output(result)
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        self.assertEqual(output["sessionTitle"], requested)
        self.assertEqual(
            self.applied_path(session_id).read_text(encoding="utf-8"),
            requested + "\n",
        )

    def test_unchanged_applied_title_only_returns_writing_guidance(self) -> None:
        session_id = "unchanged-title"
        requested = "开发中｜会话标题插件｜实现钩子运行逻辑"
        self.write_title(session_id, requested)
        first_result = self.run_hook(
            "UserPromptSubmit",
            session_id,
            session_title="Claude 自动标题",
        )
        self.assertEqual(self.hook_output(first_result)["sessionTitle"], requested)

        second_result = self.run_hook(
            "UserPromptSubmit",
            session_id,
            session_title=requested,
        )

        output = self.hook_output(second_result)
        self.assertNotIn("sessionTitle", output)
        self.assertIn(str(self.title_path(session_id)), output["additionalContext"])
        self.assertIn("标题仍准确时不要写", output["additionalContext"])
        self.assertEqual(
            self.applied_path(session_id).read_text(encoding="utf-8"),
            requested + "\n",
        )

    def test_missing_title_file_only_returns_writing_guidance(self) -> None:
        session_id = "missing-title"

        result = self.run_hook(
            "UserPromptSubmit",
            session_id,
            session_title="Claude 自动标题",
        )

        self.assertIsNotNone(result)
        output = self.hook_output(result)
        self.assertNotIn("sessionTitle", output)
        self.assertIn(str(self.title_path(session_id)), output["additionalContext"])
        self.assertFalse(self.applied_path(session_id).exists())

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

    def test_external_rename_marks_source_unconfirmed_and_preserves_name(self) -> None:
        session_id = "external-rename"
        first_title = "开发中｜会话标题插件｜建立标题基线"
        self.write_title(session_id, first_title)
        self.run_hook(
            "UserPromptSubmit",
            session_id,
            session_title="Claude 自动标题",
        )
        self.write_title(session_id, "测试｜会话标题插件｜保护外部改名")

        result = self.run_hook(
            "UserPromptSubmit",
            session_id,
            session_title="人工修改的名称",
        )

        output = self.hook_output(result)
        self.assertNotIn("sessionTitle", output)
        self.assertIn("来源未确认", output["additionalContext"])
        self.assertIn("保留现有名称", output["additionalContext"])
        self.assertEqual(
            self.applied_path(session_id).read_text(encoding="utf-8"),
            first_title + "\n",
        )

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
