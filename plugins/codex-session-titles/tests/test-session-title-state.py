#!/usr/bin/env python3
"""在隔离的临时 Codex 状态库中验证标题管理状态机。"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOOK = ROOT.parent / "hooks" / "session-title.sh"


class SessionTitleStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="title-state-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.codex_home = self.home / ".codex"
        self.title_dir = self.codex_home / "session-titles"
        self.codex_home.mkdir(parents=True)
        self.title_dir.mkdir()
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.database = self.codex_home / "state_5.sqlite"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, archived INTEGER)")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_thread(self, session_id: str, name: str | None = None) -> None:
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "INSERT INTO threads (id, name, archived) VALUES (?, ?, 0)",
                (session_id, name),
            )

    def set_name(self, session_id: str, name: str) -> None:
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE threads SET name = ? WHERE id = ?", (name, session_id))

    def get_name(self, session_id: str) -> str | None:
        with closing(sqlite3.connect(self.database)) as db, db:
            row = db.execute("SELECT name FROM threads WHERE id = ?", (session_id,)).fetchone()
        return row[0] if row else None

    def state_path(self, session_id: str) -> Path:
        return self.title_dir / f"{session_id}.state.json"

    def title_path(self, session_id: str) -> Path:
        return self.title_dir / f"{session_id}.txt"

    def read_state(self, session_id: str) -> dict:
        return json.loads(self.state_path(session_id).read_text(encoding="utf-8"))

    def write_title(self, session_id: str, title: str) -> None:
        self.title_path(session_id).write_text(title + "\n", encoding="utf-8")

    def run_hook(
        self,
        event: str,
        session_id: str,
        source: str | None = None,
        cwd: Path | None = None,
    ) -> dict:
        payload = {
            "hook_event_name": event,
            "session_id": session_id,
            "cwd": str(cwd or self.cwd),
        }
        if source is not None:
            payload["source"] = source
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["CODEX_HOME"] = str(self.codex_home)
        result = subprocess.run(
            ["bash", str(HOOK)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        return json.loads(result.stdout)

    def test_new_session_waits_for_stable_codex_name_then_applies_sidecar(self) -> None:
        session_id = "new-session"
        self.add_thread(session_id)

        start = self.run_hook("SessionStart", session_id, "startup")
        self.assertIn("初始化中：等待 Codex 初始标题稳定", start["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("<!--", start["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.get_name(session_id), "SessionStart must not race Codex initial naming")

        self.write_title(session_id, "开发中｜会话标题自动化｜接入标题状态机")
        self.set_name(session_id, "Codex 自动标题")
        self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "Codex 自动标题")
        self.assertTrue(self.title_path(session_id).exists(), "pending title is retained while initial name settles")

        self.set_name(session_id, "Codex 自动标题 (2)")
        self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "Codex 自动标题 (2)")
        self.assertTrue(self.title_path(session_id).exists(), "a changing app title resets the settling window")

        self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "开发中｜会话标题自动化｜接入标题状态机")
        self.assertFalse(self.title_path(session_id).exists())
        state = self.read_state(session_id)
        self.assertEqual(state["phase"], "managed")
        self.assertEqual(state["last_result"], "updated")

    def test_missing_thread_record_retries_without_losing_title(self) -> None:
        session_id = "late-thread"
        start = self.run_hook("SessionStart", session_id, "startup")
        self.assertIn("初始化中：等待 Codex 初始标题稳定", start["hookSpecificOutput"]["additionalContext"])
        self.write_title(session_id, "摸底｜历史会话标题管理｜等待会话记录建档")

        self.run_hook("Stop", session_id)
        self.assertEqual(self.read_state(session_id)["last_result"], "thread_not_ready")
        self.assertTrue(self.title_path(session_id).exists())

        self.add_thread(session_id)
        self.run_hook("Stop", session_id)
        self.set_name(session_id, "App 首次标题")
        self.run_hook("Stop", session_id)
        self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "摸底｜历史会话标题管理｜等待会话记录建档")
        self.assertFalse(self.title_path(session_id).exists())

    def test_missing_database_is_not_created(self) -> None:
        session_id = "missing-database"
        self.database.unlink()

        result = self.run_hook("SessionStart", session_id, "startup")

        self.assertFalse(self.database.exists(), "只读失败时不得创建空状态库")
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("读取暂时失败", context)
        self.assertEqual(self.read_state(session_id)["last_result"], "io_error")

    def test_empty_codex_title_uses_projectless_fallback_after_grace(self) -> None:
        session_id = "empty-auto-title"
        self.add_thread(session_id)
        self.run_hook("SessionStart", session_id, "startup")
        for _ in range(3):
            self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "摸底｜新会话")
        self.assertEqual(self.read_state(session_id)["last_result"], "updated")

    def test_resumed_untracked_session_is_adopted_after_new_prompt(self) -> None:
        session_id = "old-session"
        self.add_thread(session_id, "已有会话名称")

        result = self.run_hook("SessionStart", session_id, "resume")
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("来源未确认", context)
        self.assertFalse(self.state_path(session_id).exists())
        self.assertEqual(self.get_name(session_id), "已有会话名称")
        prompted = self.run_hook("UserPromptSubmit", session_id)
        self.assertIn("历史会话已收到新消息", prompted["hookSpecificOutput"]["additionalContext"])
        self.assertIn("标题文件：", prompted["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.read_state(session_id)["phase"], "initializing")
        self.assertEqual(self.get_name(session_id), "已有会话名称")

        self.write_title(session_id, "开发中｜历史会话标题管理｜重新纳入标题更新")
        self.run_hook("Stop", session_id)
        self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "开发中｜历史会话标题管理｜重新纳入标题更新")
        self.assertFalse(self.title_path(session_id).exists())

    def test_legacy_applied_baseline_migrates_only_when_name_matches(self) -> None:
        managed_id = "legacy-managed"
        self.add_thread(managed_id, "旧钩子标题")
        (self.title_dir / f"{managed_id}.applied").write_text("旧钩子标题\n", encoding="utf-8")
        managed = self.run_hook("SessionStart", managed_id, "resume")
        self.assertIn("标题文件：", managed["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.read_state(managed_id)["phase"], "managed")

        changed_id = "legacy-name-conflict"
        self.add_thread(changed_id, "Codex 新名称")
        (self.title_dir / f"{changed_id}.applied").write_text("旧钩子标题\n", encoding="utf-8")
        unconfirmed = self.run_hook("SessionStart", changed_id, "resume")
        self.assertIn("来源未确认", unconfirmed["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(self.state_path(changed_id).exists())
        self.assertEqual(self.get_name(changed_id), "Codex 新名称")

        prompted = self.run_hook("UserPromptSubmit", changed_id)
        self.assertIn("历史会话已收到新消息", prompted["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.read_state(changed_id)["phase"], "initializing")
        self.assertEqual(self.get_name(changed_id), "Codex 新名称")

    def test_name_change_after_adoption_is_not_called_manual_but_never_overwritten(self) -> None:
        session_id = "renamed-session"
        self.add_thread(session_id, "App 初始标题")
        self.run_hook("SessionStart", session_id, "startup")
        self.write_title(session_id, "方案｜会话标题自动化｜设计三段标题格式")
        self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "方案｜会话标题自动化｜设计三段标题格式")

        self.set_name(session_id, "外部改名")
        self.write_title(session_id, "测试｜会话标题自动化｜验证不覆盖外部名称")
        result = self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "外部改名")
        self.assertEqual(self.read_state(session_id)["phase"], "source_unconfirmed")
        self.assertEqual(self.read_state(session_id)["last_result"], "name_conflict")
        self.assertTrue(self.title_path(session_id).exists(), "conflicting title is retained for review")
        self.assertNotIn("用户管理", result.get("systemMessage", ""))
        prompted = self.run_hook("UserPromptSubmit", session_id)
        self.assertIn("来源未确认", prompted["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("标题文件：", prompted["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.get_name(session_id), "外部改名")

    def test_invalid_title_is_retained_until_corrected(self) -> None:
        session_id = "invalid-sidecar-title"
        self.add_thread(session_id, "App 初始标题")
        self.run_hook("SessionStart", session_id, "startup")
        self.write_title(session_id, "新状态词｜会话标题自动化｜不在白名单")
        self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "App 初始标题")
        self.assertEqual(self.read_state(session_id)["last_result"], "invalid_title")
        self.assertTrue(self.title_path(session_id).exists())

    def test_database_write_failure_keeps_title_for_retry(self) -> None:
        session_id = "retry-session"
        self.add_thread(session_id, "App 初始标题")
        self.run_hook("SessionStart", session_id, "startup")
        self.write_title(session_id, "测试｜会话标题自动化｜暂时写入失败")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "CREATE TRIGGER fail_title_update BEFORE UPDATE OF name ON threads "
                "BEGIN SELECT RAISE(ABORT, 'simulated database write failure'); END"
            )

        self.run_hook("Stop", session_id)
        state = self.read_state(session_id)
        self.assertEqual(state["last_result"], "io_error")
        self.assertEqual(state["phase"], "retryable_error")
        self.assertIsNotNone(state.get("pending_update"))
        self.assertTrue(self.title_path(session_id).exists())
        self.assertEqual(self.get_name(session_id), "App 初始标题")

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP TRIGGER fail_title_update")
        self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "测试｜会话标题自动化｜暂时写入失败")
        self.assertFalse(self.title_path(session_id).exists())
        self.assertIsNone(self.read_state(session_id).get("pending_update"))

    def test_worktree_prefix_is_kept_when_applying_file_title(self) -> None:
        repo = self.root / "repo"
        worktree = self.root / "worktree"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "--allow-empty", "-q", "-m", "base"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feature-title", str(worktree), "main"],
            check=True,
        )
        session_id = "worktree-session"
        self.add_thread(session_id, "Codex 自动标题")
        self.run_hook("SessionStart", session_id, "startup", worktree)
        self.write_title(session_id, "开发中｜会话标题自动化｜工作树标题同步")
        self.run_hook("Stop", session_id, cwd=worktree)
        self.assertEqual(self.get_name(session_id), "[工作树] 开发中｜会话标题自动化｜工作树标题同步")

    def test_requested_title_requires_three_nonempty_parts(self) -> None:
        session_id = "three-part-title"
        self.add_thread(session_id, "App 初始标题")
        self.run_hook("SessionStart", session_id, "startup")

        self.write_title(session_id, "开发中｜只写了当前进展")
        self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "App 初始标题")
        self.assertTrue(self.title_path(session_id).exists())

        self.write_title(session_id, "开发中｜会话标题自动化｜保留最初需求名称")
        self.run_hook("Stop", session_id)
        self.assertEqual(
            self.get_name(session_id),
            "开发中｜会话标题自动化｜保留最初需求名称",
        )
        self.assertFalse(self.title_path(session_id).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
