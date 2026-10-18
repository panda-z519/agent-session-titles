#!/usr/bin/env python3
"""在隔离的临时 Codex 状态库中验证标题管理状态机。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import sqlite3
import struct
import subprocess
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HOOK = ROOT.parent / "hooks" / "session-title.sh"
# RFC 6455 固定值；下方测试用 RFC 示例校验，避免钩子和模拟服务端共用同一个错误常量。
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeAppServer:
    """模拟 Codex 后台 app-server 的控制套接字：WebSocket 握手后处理 JSON-RPC。"""

    def __init__(self, socket_path: Path, database: Path, fail_rename: bool = False) -> None:
        self.database = database
        self.fail_rename = fail_rename
        self.calls: list[tuple[str, dict]] = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(socket_path))
        self.listener.listen(4)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.listener.close()

    def serve(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection:
                try:
                    self.handle(connection)
                except (OSError, EOFError):
                    pass

    @staticmethod
    def recv_exact(connection: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def handle(self, connection: socket.socket) -> None:
        request = b""
        while b"\r\n\r\n" not in request:
            request += connection.recv(4096)
        key = next(
            line.split(b":", 1)[1].strip()
            for line in request.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key:")
        )
        accept = base64.b64encode(hashlib.sha1(key + WEBSOCKET_GUID.encode()).digest())
        connection.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        while True:
            first, second = self.recv_exact(connection, 2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self.recv_exact(connection, 2))[0]
            mask = self.recv_exact(connection, 4)
            payload = bytes(
                byte ^ mask[index % 4]
                for index, byte in enumerate(self.recv_exact(connection, size))
            )
            message = json.loads(payload)
            method = message.get("method")
            self.calls.append((method, message.get("params") or {}))
            if "id" not in message:
                continue
            # 先插入一条无关广播，验证客户端只认自己请求的响应。
            self.send(connection, {"method": "remoteControl/status/changed", "params": {}})
            if method == "thread/name/set":
                if self.fail_rename:
                    self.send(connection, {"id": message["id"], "error": {"message": "boom"}})
                    continue
                params = message["params"]
                with closing(sqlite3.connect(self.database)) as db, db:
                    db.execute(
                        "UPDATE threads SET name = ? WHERE id = ?",
                        (params["name"], params["threadId"]),
                    )
            self.send(connection, {"id": message["id"], "result": {}})

    @staticmethod
    def send(connection: socket.socket, message: dict) -> None:
        payload = json.dumps(message).encode()
        connection.sendall(struct.pack("!BB", 0x81, len(payload)) + payload)


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

    def test_new_session_waits_for_stable_codex_name_without_title_file(self) -> None:
        session_id = "new-session"
        self.add_thread(session_id)

        start = self.run_hook("SessionStart", session_id, "startup")
        self.assertIn("初始化中：等待 Codex 初始标题稳定", start["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("<!--", start["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.get_name(session_id), "SessionStart must not race Codex initial naming")

        self.set_name(session_id, "Codex 自动标题")
        self.run_hook("Stop", session_id)
        self.set_name(session_id, "Codex 自动标题 (2)")
        self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "Codex 自动标题 (2)")
        self.assertEqual(self.read_state(session_id)["phase"], "initializing",
                         "a changing app title resets the settling window")

    def test_title_file_takes_over_initial_name_immediately(self) -> None:
        session_id = "new-session-title-file"
        self.add_thread(session_id)
        self.run_hook("SessionStart", session_id, "startup")

        # 标题文件到达即视为明确意图，以当时可见名称为基线直接接管，不再等待稳定
        self.write_title(session_id, "开发中｜会话标题自动化｜接入标题状态机")
        self.set_name(session_id, "Codex 自动标题")
        self.run_hook("Stop", session_id)

        self.assertEqual(self.get_name(session_id), "开发中｜会话标题自动化｜接入标题状态机")
        self.assertFalse(self.title_path(session_id).exists())
        state = self.read_state(session_id)
        self.assertEqual(state["phase"], "managed")
        self.assertEqual(state["last_result"], "updated")

    def test_title_file_before_codex_names_thread_is_applied_and_survives_auto_name(self) -> None:
        # 复现 CLI 真实时序：Agent 写入标题时 Codex 尚未起名，首轮结束后才生成名字
        session_id = "title-before-auto-name"
        self.add_thread(session_id)
        self.run_hook("SessionStart", session_id, "startup")
        self.write_title(session_id, "聊天｜聊聊今天的状态｜轻松回应")
        self.run_hook("PostToolUse", session_id)
        self.assertEqual(self.get_name(session_id), "聊天｜聊聊今天的状态｜轻松回应")
        self.assertTrue(self.read_state(session_id)["auto_name_guard"])

        self.run_hook("Stop", session_id)
        self.set_name(session_id, "记录今日状态")
        self.run_hook("UserPromptSubmit", session_id)

        self.assertEqual(self.get_name(session_id), "聊天｜聊聊今天的状态｜轻松回应")
        state = self.read_state(session_id)
        self.assertEqual(state["phase"], "managed")
        self.assertFalse(state["auto_name_guard"])
        # 守护只生效一次，之后的外部改名仍按原规则保护
        self.set_name(session_id, "用户手动改名")
        self.run_hook("UserPromptSubmit", session_id)
        self.assertEqual(self.get_name(session_id), "用户手动改名")

    def test_auto_name_guard_clears_when_codex_does_not_overwrite(self) -> None:
        session_id = "guard-no-overwrite"
        self.add_thread(session_id)
        self.run_hook("SessionStart", session_id, "startup")
        self.write_title(session_id, "开发中｜会话标题自动化｜接管空名称")
        self.run_hook("PostToolUse", session_id)
        self.run_hook("UserPromptSubmit", session_id)

        self.assertEqual(self.get_name(session_id), "开发中｜会话标题自动化｜接管空名称")
        self.assertFalse(self.read_state(session_id)["auto_name_guard"])

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

    def test_user_prompt_compensates_pending_title_and_preserves_external_rename(self) -> None:
        session_id = "prompt-compensation"
        self.add_thread(session_id, "App 初始标题")
        self.run_hook("SessionStart", session_id, "startup")
        self.run_hook("Stop", session_id)
        self.assertEqual(self.read_state(session_id)["phase"], "managed")

        desired = "修复｜会话标题自动化｜补偿未执行的停止钩子"
        self.write_title(session_id, desired)
        compensated = self.run_hook("UserPromptSubmit", session_id)

        self.assertEqual(self.get_name(session_id), desired)
        self.assertFalse(self.title_path(session_id).exists())
        self.assertIn(
            f"当前会话名：{desired}",
            compensated["hookSpecificOutput"]["additionalContext"],
        )

        self.set_name(session_id, "外部改名")
        self.write_title(session_id, "测试｜会话标题自动化｜不得覆盖外部名称")
        conflicted = self.run_hook("UserPromptSubmit", session_id)

        self.assertEqual(self.get_name(session_id), "外部改名")
        self.assertTrue(self.title_path(session_id).exists())
        self.assertEqual(self.read_state(session_id)["phase"], "source_unconfirmed")
        self.assertIn("来源未确认", conflicted["hookSpecificOutput"]["additionalContext"])

    def test_invalid_title_is_retained_until_corrected(self) -> None:
        session_id = "invalid-sidecar-title"
        self.add_thread(session_id, "App 初始标题")
        self.run_hook("SessionStart", session_id, "startup")
        self.write_title(session_id, "新状态词｜会话标题自动化｜不在白名单")
        self.run_hook("Stop", session_id)
        self.assertEqual(self.get_name(session_id), "App 初始标题")
        self.assertEqual(self.read_state(session_id)["last_result"], "invalid_title")
        self.assertTrue(self.title_path(session_id).exists())

    def test_chinese_context_lists_statuses_and_reports_rejected_title(self) -> None:
        # 复现真实会话：中文提示不列状态词，Agent 写了「进行中」被静默拒绝
        session_id = "chat-invalid-status"
        self.add_thread(session_id)
        start = self.run_hook("SessionStart", session_id, "startup")
        context = start["hookSpecificOutput"]["additionalContext"]
        self.assertIn("状态从聊天、摸底、方案", context)
        self.assertIn("即使本轮只是聊天", context)

        self.write_title(session_id, "进行中｜聊聊身体近况｜已收到近况")
        self.set_name(session_id, "记录近期身体状况")
        self.run_hook("PostToolUse", session_id)
        self.assertEqual(self.read_state(session_id)["last_result"], "invalid_title")
        prompt = self.run_hook("UserPromptSubmit", session_id)
        self.assertIn("未生效", prompt["hookSpecificOutput"]["additionalContext"])

        self.write_title(session_id, "聊天｜聊聊身体近况｜已收到近况")
        self.run_hook("PostToolUse", session_id)
        self.assertEqual(self.get_name(session_id), "聊天｜聊聊身体近况｜已收到近况")
        after = self.run_hook("UserPromptSubmit", session_id)
        self.assertNotIn("未生效", after["hookSpecificOutput"]["additionalContext"])

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

    def test_worktree_session_uses_plain_three_part_title(self) -> None:
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
        # 旧版遗留的前缀写法也能解析，但应用后只保留三段
        self.write_title(session_id, "[工作树] 开发中｜会话标题自动化｜工作树标题同步")
        self.run_hook("Stop", session_id, cwd=worktree)
        self.assertEqual(self.get_name(session_id), "开发中｜会话标题自动化｜工作树标题同步")

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

    def start_fake_app_server(self, fail_rename: bool = False) -> FakeAppServer:
        # Unix 套接字路径有长度上限，实际文件放在短路径，再像 Codex 一样用符号链接指过去。
        socket_dir = Path(tempfile.mkdtemp(prefix="ast-", dir="/tmp"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(socket_dir)], check=False))
        socket_path = socket_dir / "control.sock"
        server = FakeAppServer(socket_path, self.database, fail_rename)
        self.addCleanup(server.close)
        control_dir = self.codex_home / "app-server-control"
        control_dir.mkdir()
        (control_dir / "app-server-control.sock").symlink_to(socket_path)
        return server

    def test_websocket_guid_matches_rfc_example(self) -> None:
        accept = base64.b64encode(
            hashlib.sha1(("dGhlIHNhbXBsZSBub25jZQ==" + WEBSOCKET_GUID).encode()).digest()
        )
        self.assertEqual(accept, b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_running_app_server_receives_rename_so_open_cli_refreshes(self) -> None:
        session_id = "daemon-session"
        self.add_thread(session_id, "Codex 自动标题")
        server = self.start_fake_app_server()
        self.run_hook("SessionStart", session_id, "startup")

        self.write_title(session_id, "开发中｜会话标题自动化｜同步打开的终端窗口")
        self.run_hook("PostToolUse", session_id)

        self.assertIn(
            ("thread/name/set", {
                "threadId": session_id,
                "name": "开发中｜会话标题自动化｜同步打开的终端窗口",
            }),
            server.calls,
        )
        self.assertEqual(server.calls[0][0], "initialize")
        self.assertEqual(self.get_name(session_id), "开发中｜会话标题自动化｜同步打开的终端窗口")
        self.assertFalse(self.title_path(session_id).exists())
        self.assertEqual(self.read_state(session_id)["last_result"], "updated")

    def test_app_server_error_falls_back_to_database(self) -> None:
        session_id = "daemon-error-session"
        self.add_thread(session_id, "Codex 自动标题")
        server = self.start_fake_app_server(fail_rename=True)
        self.run_hook("SessionStart", session_id, "startup")

        self.write_title(session_id, "修复｜会话标题自动化｜服务端失败时兜底")
        self.run_hook("Stop", session_id)

        self.assertIn("thread/name/set", [method for method, _ in server.calls])
        self.assertEqual(self.get_name(session_id), "修复｜会话标题自动化｜服务端失败时兜底")
        self.assertFalse(self.title_path(session_id).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
