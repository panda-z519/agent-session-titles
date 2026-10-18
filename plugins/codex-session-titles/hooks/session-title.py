#!/usr/bin/env python3
"""在 Codex 生命周期钩子中观察初始标题并同步状态文件。"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any, TextIO

DEFAULT_LANGUAGE = "zh"
SUPPORTED_LANGUAGES = {"zh", "en"}
VALID_STATUSES = {
    "zh": {"摸底", "方案", "开发中", "测试", "修复", "待确认", "已完成", "搁置"},
    "en": {
        "Scoping",
        "Planning",
        "Building",
        "Testing",
        "Fixing",
        "Awaiting confirmation",
        "Completed",
        "Paused",
    },
}
TITLE_SEGMENTS = ("status", "initial_request", "progress")
TITLE_SEPARATORS = {"zh": "｜", "en": " | "}
WORKTREE_PREFIXES = {"zh": "[工作树] ", "en": "[worktree] "}
PROJECTLESS_TITLES = {"zh": "摸底｜新会话", "en": "Scoping | New session"}
NO_BRANCH_NAMES = {"zh": "无分支", "en": "no branch"}
WORKTREE_PREFIX_PATTERN = re.compile(r"^(?:(?:\[工作树\]|\[worktree\])\s*)+")
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
PHASE_INITIALIZING = "initializing"
PHASE_MANAGED = "managed"
PHASE_SOURCE_UNCONFIRMED = "source_unconfirmed"
PHASE_RETRYABLE_ERROR = "retryable_error"
RESULT_UPDATED = "updated"
RESULT_THREAD_NOT_READY = "thread_not_ready"
RESULT_NAME_CONFLICT = "name_conflict"
RESULT_IO_ERROR = "io_error"
RESULT_INITIAL_TITLE_PENDING = "initial_title_pending"
RESULT_INITIAL_TITLE_STABLE = "initial_title_stable"
RESULT_INITIAL_TITLE_TIMEOUT = "initial_title_timeout"
INITIAL_EMPTY_STOP_LIMIT = 3
SESSION_LOCK_TIMEOUT_SECONDS = 3.0
SESSION_LOCK_POLL_SECONDS = 0.05
APP_SERVER_TIMEOUT_SECONDS = 3.0
APP_SERVER_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def read_language(config_file: Path) -> str:
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return DEFAULT_LANGUAGE
    if not isinstance(config, dict) or config.get("language") not in SUPPORTED_LANGUAGES:
        return DEFAULT_LANGUAGE
    return config["language"]


def project_context(cwd: str, language: str) -> tuple[bool, str, str, str]:
    worktree_prefix = ""
    try:
        git_dir = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--git-dir"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        common_dir = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        git_path = Path(cwd, git_dir).resolve()
        common_path = Path(cwd, common_dir).resolve()
        if git_path != common_path:
            worktree_prefix = WORKTREE_PREFIXES[language]
        project = common_path.parent.name
        branch = subprocess.check_output(
            ["git", "-C", cwd, "branch", "--show-current"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip() or NO_BRANCH_NAMES[language]
    except (OSError, subprocess.CalledProcessError):
        return False, "", "", ""
    return True, worktree_prefix, project, branch


def read_thread_name(database: Path, session_id: str) -> tuple[str, str]:
    try:
        database_uri = f"{database.resolve().as_uri()}?mode=rw"
        with closing(sqlite3.connect(database_uri, uri=True, timeout=3)) as connection:
            connection.execute("PRAGMA busy_timeout = 3000")
            row = connection.execute(
                "SELECT name FROM threads WHERE id = ? AND archived = 0",
                (session_id,),
            ).fetchone()
        if row is None:
            return RESULT_THREAD_NOT_READY, ""
        return "ok", (row[0] or "").strip()
    except sqlite3.Error:
        return RESULT_IO_ERROR, ""


class AppServerUnavailable(Exception):
    """后台 app-server 不可用或返回异常，调用方改用 SQLite 兜底。"""


class AppServerConnection:
    """通过 Codex 控制套接字（Unix 域套接字上的 WebSocket）发送 JSON-RPC 的最小客户端。"""

    def __init__(self, socket_path: Path, timeout: float) -> None:
        self.deadline = time.monotonic() + timeout
        self.buffer = b""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(timeout)
            self.sock.connect(str(socket_path))
            self._handshake()
        except BaseException:
            self.sock.close()
            raise

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _recv_some(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerUnavailable("timeout")
        self.sock.settimeout(remaining)
        chunk = self.sock.recv(65536)
        if not chunk:
            raise AppServerUnavailable("connection closed")
        self.buffer += chunk

    def _recv_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            self._recv_some()
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock.sendall(
            (
                "GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > 65536:
                raise AppServerUnavailable("handshake too large")
            self._recv_some()
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        if head.split(b"\r\n", 1)[0].split()[1:2] != [b"101"]:
            raise AppServerUnavailable("handshake rejected")
        accept = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
        )
        if accept not in head:
            raise AppServerUnavailable("handshake accept mismatch")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | size)
        elif size < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, size)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, size)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send(self, message: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(message, ensure_ascii=False).encode("utf-8"))

    def _read_message(self) -> bytes:
        fragments: list[bytes] = []
        while True:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._recv_exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._recv_exact(8))[0]
            if size > APP_SERVER_MAX_MESSAGE_BYTES:
                raise AppServerUnavailable("message too large")
            mask = self._recv_exact(4) if second & 0x80 else b""
            payload = self._recv_exact(size)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise AppServerUnavailable("connection closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode in {0x1, 0x2, 0x0}:
                fragments.append(payload)
                if first & 0x80:
                    return b"".join(fragments)

    def request(self, request_id: int, method: str, params: dict[str, Any]) -> Any:
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            try:
                message = json.loads(self._read_message().decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as error:
                raise AppServerUnavailable("invalid message") from error
            # 其他会话的广播通知会混在同一连接里，只认本请求的响应。
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message or "result" not in message:
                raise AppServerUnavailable("request failed")
            return message["result"]


def set_thread_name_via_app_server(codex_home: Path, session_id: str, name: str) -> bool:
    """经正在运行的后台 app-server 改名，使已打开的 CLI 窗口收到 ThreadNameUpdated 通知。"""
    # 控制套接字通常是指向短路径的符号链接；先解析，避免超出 Unix 套接字路径长度上限。
    socket_path = (codex_home / "app-server-control" / "app-server-control.sock").resolve()
    if not socket_path.exists():
        return False
    try:
        connection = AppServerConnection(socket_path, APP_SERVER_TIMEOUT_SECONDS)
    except (OSError, AppServerUnavailable):
        return False
    try:
        connection.request(1, "initialize", {
            "clientInfo": {
                "name": "agent-session-titles",
                "title": None,
                "version": "1",
            }
        })
        connection.send({"method": "initialized"})
        connection.request(2, "thread/name/set", {"threadId": session_id, "name": name})
        return True
    except (OSError, AppServerUnavailable):
        return False
    finally:
        connection.close()


def compare_and_set_thread_name(
    database: Path,
    session_id: str,
    expected: str,
    desired: str,
) -> str:
    result, current = read_thread_name(database, session_id)
    if result != "ok":
        return result
    if current != expected:
        return RESULT_NAME_CONFLICT
    # 优先交给后台 app-server：它同时更新状态库和会话索引，并通知已打开的 CLI 窗口刷新。
    # 不能在持有 SQLite 写锁时调用，否则服务端写库会被本进程阻塞。
    if set_thread_name_via_app_server(database.parent, session_id, desired):
        return RESULT_UPDATED
    return compare_and_set_thread_name_in_database(database, session_id, expected, desired)


def compare_and_set_thread_name_in_database(
    database: Path,
    session_id: str,
    expected: str,
    desired: str,
) -> str:
    try:
        database_uri = f"{database.resolve().as_uri()}?mode=rw"
        with closing(sqlite3.connect(database_uri, uri=True, timeout=3)) as connection:
            connection.execute("PRAGMA busy_timeout = 3000")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT name FROM threads WHERE id = ? AND archived = 0",
                (session_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return RESULT_THREAD_NOT_READY
            current = (row[0] or "").strip()
            if current != expected:
                connection.rollback()
                return RESULT_NAME_CONFLICT
            cursor = connection.execute(
                "UPDATE threads SET name = ? "
                "WHERE id = ? AND archived = 0 AND COALESCE(name, '') = ?",
                (desired, session_id, expected),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return RESULT_NAME_CONFLICT
            connection.commit()
            return RESULT_UPDATED
    except sqlite3.Error:
        return RESULT_IO_ERROR


def load_state(state_file: Path) -> tuple[str, dict[str, Any] | None]:
    if not state_file.exists():
        return "missing", None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return RESULT_IO_ERROR, None
    if not isinstance(state, dict) or state.get("version") != 1:
        return RESULT_IO_ERROR, None
    # v1 旧状态没有逐段保护字段；只有已由插件写入的三段标题才可安全补齐。
    if "segment_protection_active" not in state:
        parsed = parse_three_part_title(state.get("last_applied_name"))
        active = parsed is not None and state.get("last_result") in {
            RESULT_UPDATED,
            "legacy_baseline_migrated",
        }
        state["segment_protection_active"] = active
        state["locked_segments"] = ["initial_request"] if active else []
    elif not isinstance(state.get("locked_segments"), list):
        state["locked_segments"] = (
            ["initial_request"] if state.get("segment_protection_active") else []
        )
    if "initial_takeover_pending" not in state:
        state["initial_takeover_pending"] = False
    return "ok", state


def write_state(state_file: Path, state: dict[str, Any]) -> bool:
    temp_path: Path | None = None
    try:
        state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=state_file.parent,
            prefix=f".{state_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(state, temp_file, ensure_ascii=False, separators=(",", ":"))
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, state_file)
        return True
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def acquire_session_lock(lock_file: Path) -> TextIO | None:
    handle: TextIO | None = None
    try:
        handle = lock_file.open("a+", encoding="utf-8")
        deadline = time.monotonic() + SESSION_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except InterruptedError:
                continue
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    return None
                time.sleep(SESSION_LOCK_POLL_SECONDS)
    except OSError:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        return None


def release_session_lock(handle: TextIO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass


def read_applied(applied_file: Path) -> str:
    try:
        return applied_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def new_initial_state(
    session_id: str,
    result: str,
    name: str,
    origin: str = "startup",
) -> dict[str, Any]:
    return {
        "version": 1,
        "session_id": session_id,
        "phase": PHASE_INITIALIZING,
        "origin": origin,
        "candidate_name": name or None,
        "initial_observations": 1 if name else 0,
        "empty_stop_count": 0,
        "last_applied_name": None,
        "segment_protection_active": False,
        "locked_segments": [],
        "initial_takeover_pending": False,
        "pending_update": None,
        "last_result": result if result != "ok" else RESULT_INITIAL_TITLE_PENDING,
        "last_error": None,
    }


def managed_state(session_id: str, name: str, result: str) -> dict[str, Any]:
    segment_protection_active = parse_three_part_title(name) is not None
    return {
        "version": 1,
        "session_id": session_id,
        "phase": PHASE_MANAGED,
        "candidate_name": name,
        "initial_observations": 0,
        "empty_stop_count": 0,
        "last_applied_name": name,
        "segment_protection_active": segment_protection_active,
        "locked_segments": ["initial_request"] if segment_protection_active else [],
        "initial_takeover_pending": False,
        "pending_update": None,
        "last_result": result,
        "last_error": None,
    }


def resolve_state(
    event: str,
    source: str,
    session_id: str,
    database: Path,
    state_file: Path,
    applied_file: Path,
) -> tuple[str, dict[str, Any] | None]:
    load_result, state = load_state(state_file)
    if load_result == "ok" and state is not None:
        if state.get("session_id") == session_id:
            return "ok", state
        return RESULT_IO_ERROR, None
    if load_result == RESULT_IO_ERROR:
        return RESULT_IO_ERROR, None

    applied = read_applied(applied_file)
    if applied:
        result, current = read_thread_name(database, session_id)
        if result == RESULT_THREAD_NOT_READY:
            state = managed_state(session_id, applied, RESULT_THREAD_NOT_READY)
            return ("ok", state) if write_state(state_file, state) else (RESULT_IO_ERROR, None)
        if result == RESULT_IO_ERROR:
            return RESULT_IO_ERROR, None
        if current == applied:
            state = managed_state(session_id, applied, "legacy_baseline_migrated")
            return ("ok", state) if write_state(state_file, state) else (RESULT_IO_ERROR, None)
        return PHASE_SOURCE_UNCONFIRMED, None

    if event == "SessionStart" and source == "startup":
        result, current = read_thread_name(database, session_id)
        state = new_initial_state(session_id, result, current)
        return ("ok", state) if write_state(state_file, state) else (RESULT_IO_ERROR, None)

    # 恢复的旧会话没有钩子基线时保守观察，不声称标题一定是用户手动设置的。
    return PHASE_SOURCE_UNCONFIRMED, None


def observe_initial_name(
    state: dict[str, Any],
    current: str,
    event: str,
) -> bool:
    if current:
        candidate = state.get("candidate_name")
        if current != candidate:
            state["candidate_name"] = current
            state["initial_observations"] = 1
        elif event == "Stop":
            state["initial_observations"] = int(state.get("initial_observations", 0)) + 1
        state["empty_stop_count"] = 0
        if event == "Stop" and state["initial_observations"] >= 2:
            state["phase"] = PHASE_MANAGED
            state["last_applied_name"] = current
            state["segment_protection_active"] = False
            state["locked_segments"] = []
            state["last_result"] = RESULT_INITIAL_TITLE_STABLE
            state["last_error"] = None
            return True
        state["last_result"] = RESULT_INITIAL_TITLE_PENDING
        return False

    state["candidate_name"] = None
    state["initial_observations"] = 0
    if event == "Stop":
        state["empty_stop_count"] = int(state.get("empty_stop_count", 0)) + 1
        if state["empty_stop_count"] >= INITIAL_EMPTY_STOP_LIMIT:
            state["phase"] = PHASE_MANAGED
            state["last_applied_name"] = ""
            state["segment_protection_active"] = False
            state["locked_segments"] = []
            state["last_result"] = RESULT_INITIAL_TITLE_TIMEOUT
            state["last_error"] = None
            return True
    state["last_result"] = RESULT_INITIAL_TITLE_PENDING
    return False


def read_requested_title(title_file: Path) -> tuple[str, str, str]:
    try:
        content = title_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", "", ""
    except (OSError, UnicodeError):
        return RESULT_IO_ERROR, "", ""
    lines = content.splitlines()
    first_line = lines[0] if lines else ""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", first_line).strip()[:100]
    return "ok", cleaned, content


def parse_three_part_title(value: Any) -> tuple[str, str, tuple[str, str, str]] | None:
    if not isinstance(value, str):
        return None
    prefix_match = WORKTREE_PREFIX_PATTERN.match(value.strip())
    prefix = prefix_match.group(0) if prefix_match else ""
    title = WORKTREE_PREFIX_PATTERN.sub("", value).strip()
    matches: list[tuple[str, str, tuple[str, str, str]]] = []
    for language, separator in TITLE_SEPARATORS.items():
        parts = tuple(part.strip() for part in title.split(separator))
        if len(parts) == 3 and all(parts):
            matches.append((language, prefix, parts))
    return matches[0] if len(matches) == 1 else None


def format_requested_title(value: str, worktree_prefix: str, language: str) -> str:
    title = WORKTREE_PREFIX_PATTERN.sub("", value).strip()
    separator = TITLE_SEPARATORS[language]
    parts = tuple(part.strip() for part in title.split(separator))
    if len(parts) != 3 or any(not part for part in parts):
        return ""
    return f"{worktree_prefix}{separator.join(parts)}"[:60]


def protected_segments(state: dict[str, Any]) -> set[str]:
    values = state.get("locked_segments", [])
    if not isinstance(values, list):
        return set()
    return {value for value in values if value in TITLE_SEGMENTS}


def mark_source_unconfirmed(
    state: dict[str, Any],
    state_file: Path,
    current: str,
) -> str:
    state["phase"] = PHASE_SOURCE_UNCONFIRMED
    state["pending_update"] = None
    state["initial_takeover_pending"] = False
    state["last_result"] = RESULT_NAME_CONFLICT
    state["observed_name"] = current
    state["last_error"] = None
    return RESULT_NAME_CONFLICT if write_state(state_file, state) else RESULT_IO_ERROR


def reconcile_visible_title(
    state: dict[str, Any],
    state_file: Path,
    current: str,
) -> str:
    expected = state.get("last_applied_name")
    if isinstance(expected, str) and current == expected:
        return "ok"
    if not state.get("segment_protection_active"):
        return mark_source_unconfirmed(state, state_file, current)

    expected_title = parse_three_part_title(expected)
    current_title = parse_three_part_title(current)
    if (
        expected_title is None
        or current_title is None
        or expected_title[0] != current_title[0]
    ):
        return mark_source_unconfirmed(state, state_file, current)

    locked = protected_segments(state)
    for segment, expected_part, current_part in zip(
        TITLE_SEGMENTS, expected_title[2], current_title[2]
    ):
        if expected_part != current_part:
            locked.add(segment)
    state["phase"] = PHASE_MANAGED
    state["last_applied_name"] = current
    state["candidate_name"] = current
    state["locked_segments"] = [
        segment for segment in TITLE_SEGMENTS if segment in locked
    ]
    state["last_result"] = "external_segments_locked"
    state["last_error"] = None
    return "ok" if write_state(state_file, state) else RESULT_IO_ERROR


def merge_protected_title(
    state: dict[str, Any],
    current: str,
    requested: str,
) -> str:
    if not state.get("segment_protection_active"):
        return requested
    current_title = parse_three_part_title(current)
    requested_title = parse_three_part_title(requested)
    if current_title is None or requested_title is None:
        return ""
    locked = protected_segments(state)
    parts = list(requested_title[2])
    for index, segment in enumerate(TITLE_SEGMENTS):
        if segment in locked:
            parts[index] = current_title[2][index]
    return f"{requested_title[1]}{TITLE_SEPARATORS[requested_title[0]].join(parts)}"


def automatic_status_is_valid(
    state: dict[str, Any],
    title: str,
    language: str,
) -> bool:
    parsed = parse_three_part_title(title)
    if parsed is None or parsed[0] != language:
        return False
    if "status" in protected_segments(state):
        return True
    return parsed[2][0] in VALID_STATUSES[language]


def finish_pending_update(
    state: dict[str, Any],
    state_file: Path,
    title_file: Path,
    pending: dict[str, str],
) -> str:
    state["phase"] = PHASE_MANAGED
    state["last_applied_name"] = pending["desired_name"]
    state["candidate_name"] = pending["desired_name"]
    state["initial_takeover_pending"] = False
    if parse_three_part_title(pending["desired_name"]) is not None:
        state["segment_protection_active"] = True
        locked = protected_segments(state)
        locked.add("initial_request")
        state["locked_segments"] = [
            segment for segment in TITLE_SEGMENTS if segment in locked
        ]
    state["pending_update"] = None
    state["last_result"] = RESULT_UPDATED
    state["last_error"] = None
    if not write_state(state_file, state):
        return RESULT_IO_ERROR
    try:
        if title_file.read_text(encoding="utf-8") == pending["title_file_content"]:
            title_file.unlink(missing_ok=True)
    except OSError:
        pass
    return RESULT_UPDATED


def recover_pending_update(
    state: dict[str, Any],
    database: Path,
    state_file: Path,
    title_file: Path,
) -> str:
    pending = state.get("pending_update")
    if not isinstance(pending, dict):
        return "ok"
    result, current = read_thread_name(database, state["session_id"])
    if result != "ok":
        state["last_result"] = result
        state["last_error"] = result
        if result == RESULT_IO_ERROR:
            state["phase"] = PHASE_RETRYABLE_ERROR
        write_state(state_file, state)
        return result
    desired = pending["desired_name"]
    expected = pending["expected_name"]
    if current == desired:
        return finish_pending_update(state, state_file, title_file, pending)
    if current != expected:
        if state.get("initial_takeover_pending"):
            if parse_three_part_title(current) is not None:
                return mark_source_unconfirmed(state, state_file, current)
            # 首次接管期间应用仍可能生成标题。先采用新基线，等下一事件确认稳定后再 CAS。
            state["last_applied_name"] = current
            state["candidate_name"] = current
            pending["expected_name"] = current
            state["pending_update"] = pending
            state["last_result"] = RESULT_NAME_CONFLICT
            state["last_error"] = None
            return (
                RESULT_NAME_CONFLICT
                if write_state(state_file, state)
                else RESULT_IO_ERROR
            )
        reconcile_result = reconcile_visible_title(state, state_file, current)
        if reconcile_result != "ok":
            return reconcile_result
        desired = merge_protected_title(state, current, desired)
        if not desired:
            return mark_source_unconfirmed(state, state_file, current)
        pending["expected_name"] = current
        pending["desired_name"] = desired
        state["pending_update"] = pending
        state["last_result"] = "update_pending"
        state["last_error"] = None
        if not write_state(state_file, state):
            return RESULT_IO_ERROR
        expected = current
    else:
        desired = merge_protected_title(state, current, desired)
        if not desired:
            return mark_source_unconfirmed(state, state_file, current)
        pending["desired_name"] = desired
        state["pending_update"] = pending
        if not write_state(state_file, state):
            return RESULT_IO_ERROR
    result = compare_and_set_thread_name(database, state["session_id"], expected, desired)
    if result == RESULT_UPDATED:
        return finish_pending_update(state, state_file, title_file, pending)
    state["last_result"] = result
    state["last_error"] = result if result == RESULT_IO_ERROR else None
    if result == RESULT_IO_ERROR:
        state["phase"] = PHASE_RETRYABLE_ERROR
    write_state(state_file, state)
    return result


def apply_requested_title(
    state: dict[str, Any],
    database: Path,
    state_file: Path,
    title_file: Path,
    worktree_prefix: str,
    fallback_title: str,
    language: str,
) -> str:
    session_id = state["session_id"]
    if state.get("pending_update"):
        return recover_pending_update(state, database, state_file, title_file)

    result, current = read_thread_name(database, session_id)
    if result != "ok":
        state["last_result"] = result
        state["last_error"] = result
        if result == RESULT_IO_ERROR:
            state["phase"] = PHASE_RETRYABLE_ERROR
        write_state(state_file, state)
        return result

    expected = state.get("last_applied_name")
    if not isinstance(expected, str):
        return mark_source_unconfirmed(state, state_file, current)
    if current != expected:
        reconcile_result = reconcile_visible_title(state, state_file, current)
        if reconcile_result != "ok":
            return reconcile_result

    file_result, file_content, file_text = read_requested_title(title_file)
    if file_result == RESULT_IO_ERROR:
        state["last_result"] = RESULT_IO_ERROR
        state["last_error"] = RESULT_IO_ERROR
        write_state(state_file, state)
        return RESULT_IO_ERROR
    use_fallback = (
        file_result == "missing"
        and state.get("last_result") == RESULT_INITIAL_TITLE_TIMEOUT
        and current == ""
    )
    if file_result == "missing" and not use_fallback:
        return "unchanged"
    requested = (
        fallback_title
        if use_fallback
        else format_requested_title(file_content, worktree_prefix, language)
    )
    if not requested:
        state["last_result"] = "invalid_title"
        state["last_error"] = "invalid_title"
        write_state(state_file, state)
        return "invalid_title"
    desired = merge_protected_title(state, current, requested)
    if not desired:
        return mark_source_unconfirmed(state, state_file, current)
    if not use_fallback and not automatic_status_is_valid(state, desired, language):
        state["last_result"] = "invalid_title"
        state["last_error"] = "invalid_title"
        write_state(state_file, state)
        return "invalid_title"
    if desired == current:
        pending = {
            "expected_name": current,
            "desired_name": desired,
            "title_file_content": file_text,
        }
        return finish_pending_update(state, state_file, title_file, pending)

    pending = {
        "expected_name": current,
        "desired_name": desired,
        "title_file_content": file_text,
    }
    state["pending_update"] = pending
    state["last_result"] = "update_pending"
    state["last_error"] = None
    if not write_state(state_file, state):
        return RESULT_IO_ERROR

    result = compare_and_set_thread_name(database, session_id, current, desired)
    if result == RESULT_UPDATED:
        return finish_pending_update(state, state_file, title_file, pending)
    state["last_result"] = result
    state["last_error"] = result if result != RESULT_NAME_CONFLICT else None
    if result == RESULT_IO_ERROR:
        state["phase"] = PHASE_RETRYABLE_ERROR
    write_state(state_file, state)
    return result


def context_for_state(
    state: dict[str, Any] | None,
    phase_result: str,
    current: str,
    title_file: Path,
    language: str,
) -> str:
    if language == "en":
        if phase_result == PHASE_SOURCE_UNCONFIRMED or (
            state and state.get("phase") == PHASE_SOURCE_UNCONFIRMED
        ):
            return (
                f'The source of the current name "{current}" is unconfirmed. '
                "Keep the existing name and do not write the title file."
            )
        if state is None:
            return (
                "The title state cannot be saved right now. Keep the existing session name "
                "and do not write the title file."
            )
        if phase_result == RESULT_IO_ERROR:
            return (
                f"The title or session state cannot be read right now; title file: {title_file}. "
                "The pending title will be kept and a later hook event will retry it."
            )
        if state.get("phase") == PHASE_INITIALIZING:
            prompt = (
                "A new message was received in this resumed session. Title management is now "
                "starting, with the current name kept as the baseline. "
                if state.get("origin") == "resumed_after_new_prompt"
                else ""
            )
            return (
                f"{prompt}Initializing: waiting for the initial Codex title to stabilize. "
                f"Current name: {current or 'Codex has not generated an initial title yet'}; "
                f"title file: {title_file}. As soon as you understand the initial request, "
                "use a file-writing tool to write one line in the format "
                '"Status | Initial request | Current progress". Choose Status from Scoping, '
                "Planning, Building, Testing, Fixing, Awaiting confirmation, Completed, or "
                "Paused. Keep the middle part unchanged once it captures the user's initial "
                "request, and update the final part whenever the phase or progress changes. "
                "Write during the work instead of waiting for the final response. The hook "
                "synchronizes after the file-writing tool completes."
            )
        if state.get("phase") == PHASE_RETRYABLE_ERROR:
            return (
                f"Title synchronization failed temporarily; title file: {title_file}. "
                "The pending title will be kept and retried on a later hook event. While "
                "continuing, do not rewrite the file if the title remains accurate."
            )
        return (
            f"Current session name: {current}; title file: {title_file}. When the topic or phase "
            "changes significantly, use a file-writing tool to write one line in the format "
            '"Status | Initial request | Current progress". Choose Status from Scoping, '
            "Planning, Building, Testing, Fixing, Awaiting confirmation, Completed, or Paused. "
            "Keep the middle part unchanged once it captures the user's initial request, and "
            "update the final part with the current progress. Write during the work instead of "
            "waiting for the final response; do not rewrite an accurate title."
        )

    if phase_result == PHASE_SOURCE_UNCONFIRMED or (state and state.get("phase") == PHASE_SOURCE_UNCONFIRMED):
        return f"当前名称「{current}」的来源未确认。保留现有名称，不写标题文件。"
    if state is None:
        return "标题状态暂时无法保存。保留现有会话名，不写标题文件。"
    if phase_result == RESULT_IO_ERROR:
        return (
            f"标题或会话状态读取暂时失败；标题文件路径：{title_file}。"
            "待处理标题会保留，后续钩子事件会重试。"
        )
    if state.get("phase") == PHASE_INITIALIZING:
        prompt = (
            "历史会话已收到新消息，现开始纳入标题管理；保留当前名称作为基线。"
            if state.get("origin") == "resumed_after_new_prompt"
            else ""
        )
        return (
            f"{prompt}初始化中：等待 Codex 初始标题稳定。"
            f"当前名称：{current or 'Codex 尚未生成初始标题'}；标题文件：{title_file}。"
            "一旦理解用户的初始需求，就用写入文件的工具写一行"
            "「状态｜初始需求｜当前进展」；阶段或进展变化时及时更新末段。"
            "中间段概括用户最初要解决的问题并保持不变。"
            "在工作过程中写入，不要等最终回复；写入工具完成后钩子会立即同步。"
        )
    if state.get("phase") == PHASE_RETRYABLE_ERROR:
        return (
            f"标题同步暂时失败；标题文件：{title_file}。"
            "待处理标题会保留并在后续钩子事件重试。"
            "继续工作时如标题仍准确，不要重复写文件。"
        )
    return (
        f"当前会话名：{current}；标题文件：{title_file}。"
        "主题或阶段明显变化时，"
        "用写入文件的工具写一行「状态｜初始需求｜当前进展」。"
        "中间段概括用户最初要解决的问题并保持不变；末段概括目前进展。"
        "在工作过程中及时写入，不要等最终回复；标题仍准确时不要重复写。"
    )


def emit(
    event: str,
    context: str = "",
    result: str = "ok",
    language: str = DEFAULT_LANGUAGE,
) -> None:
    if event in {"Stop", "PostToolUse"}:
        if result == RESULT_IO_ERROR:
            message = (
                "Session title synchronization failed temporarily. The title file has been "
                "kept and will be retried later."
                if language == "en"
                else "会话标题同步暂时失败，标题文件已保留，后续会重试。"
            )
            if event == "Stop":
                print(json.dumps({"systemMessage": message}, ensure_ascii=False))
            else:
                print("{}")
        else:
            print("{}")
    else:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": context,
            }
        }, ensure_ascii=False))


def run_session_transaction(payload: dict[str, Any]) -> int:
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "")
    if event not in {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}:
        return 0
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        return 0

    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    state_dir = home / "session-titles"
    language = read_language(state_dir / "config.json")
    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        emit(event, result=RESULT_IO_ERROR, language=language)
        return 0

    state_file = state_dir / f"{session_id}.state.json"
    title_file = state_dir / f"{session_id}.txt"
    applied_file = state_dir / f"{session_id}.applied"
    database = home / "state_5.sqlite"
    source = payload.get("source", "")

    load_result, state = load_state(state_file)
    if event == "PostToolUse" and not title_file.exists() and not (
        load_result == "ok" and state and state.get("pending_update")
    ):
        emit(event, language=language)
        return 0
    if load_result == RESULT_IO_ERROR:
        current_result, current = read_thread_name(database, session_id)
        emit(
            event,
            context_for_state(None, RESULT_IO_ERROR, current, title_file, language),
            RESULT_IO_ERROR,
            language,
        )
        return 0

    has_project, worktree_prefix, project, branch = project_context(
        payload.get("cwd") or os.getcwd(), language
    )
    fallback_title = (
        f"{worktree_prefix}{project}·{branch}"
        if has_project
        else PROJECTLESS_TITLES[language]
    )

    if load_result == "missing":
        applied = read_applied(applied_file)
        if applied:
            current_result, current = read_thread_name(database, session_id)
            if current_result == "ok" and current == applied:
                state = managed_state(session_id, applied, "legacy_baseline_migrated")
                if not write_state(state_file, state):
                    emit(event, result=RESULT_IO_ERROR, language=language)
                    return 0
                load_result = "ok"
            elif current_result == RESULT_THREAD_NOT_READY:
                state = managed_state(session_id, applied, RESULT_THREAD_NOT_READY)
                if not write_state(state_file, state):
                    emit(event, result=RESULT_IO_ERROR, language=language)
                    return 0
                load_result = "ok"
            elif event == "UserPromptSubmit" and current_result == "ok":
                # 用户在恢复的历史会话中发来新消息，才将当前名称作为基线纳入管理。
                state = new_initial_state(
                    session_id, "ok", current, "resumed_after_new_prompt"
                )
                if not write_state(state_file, state):
                    emit(event, result=RESULT_IO_ERROR, language=language)
                    return 0
                load_result = "ok"
            else:
                current = current if current_result == "ok" else ""
                emit(
                    event,
                    context_for_state(
                        None, PHASE_SOURCE_UNCONFIRMED, current, title_file, language
                    ),
                    language=language,
                )
                return 0
        elif event == "SessionStart" and source == "startup":
            current_result, current = read_thread_name(database, session_id)
            state = new_initial_state(session_id, current_result, current)
            if not write_state(state_file, state):
                emit(event, result=RESULT_IO_ERROR, language=language)
                return 0
            load_result = "ok"
        elif event == "UserPromptSubmit":
            # 无状态的恢复会话只在收到新消息时重新处理；SessionStart 不改名。
            current_result, current = read_thread_name(database, session_id)
            state = new_initial_state(
                session_id,
                current_result,
                current,
                "resumed_after_new_prompt",
            )
            if not write_state(state_file, state):
                emit(event, result=RESULT_IO_ERROR, language=language)
                return 0
            load_result = "ok"
        else:
            current_result, current = read_thread_name(database, session_id)
            if current_result != "ok":
                current = ""
            emit(
                event,
                context_for_state(
                    None, PHASE_SOURCE_UNCONFIRMED, current, title_file, language
                ),
                language=language,
            )
            return 0

    if state is None or state.get("session_id") != session_id:
        emit(event, result=RESULT_IO_ERROR, language=language)
        return 0

    if state.get("pending_update"):
        result = recover_pending_update(state, database, state_file, title_file)
        if result not in {RESULT_UPDATED, "ok", "unchanged"}:
            if event == "Stop":
                emit(event, result=result, language=language)
            else:
                current_result, current = read_thread_name(database, session_id)
                emit(
                    event,
                    context_for_state(state, result, current, title_file, language),
                    result,
                    language,
                )
            return 0

    if state.get("phase") == PHASE_INITIALIZING:
        current_result, current = read_thread_name(database, session_id)
        if current_result != "ok":
            state["last_result"] = current_result
            state["last_error"] = current_result
            if not write_state(state_file, state):
                current_result = RESULT_IO_ERROR
            if event == "Stop":
                emit(event, result=current_result, language=language)
            else:
                emit(
                    event,
                    context_for_state(
                        state, current_result, "", title_file, language
                    ),
                    current_result,
                    language,
                )
            return 0
        if title_file.exists() and current and event in {"PostToolUse", "Stop"}:
            # 明确的三段标题意图已到达；以此刻可见名称为 CAS 基线直接接管。
            state["candidate_name"] = current
            state["last_applied_name"] = current
            state["segment_protection_active"] = False
            state["locked_segments"] = []
            state["initial_takeover_pending"] = True
            state["last_result"] = RESULT_INITIAL_TITLE_STABLE
            state["last_error"] = None
            if not write_state(state_file, state):
                emit(event, result=RESULT_IO_ERROR, language=language)
                return 0
            result = apply_requested_title(
                state,
                database,
                state_file,
                title_file,
                worktree_prefix,
                fallback_title,
                language,
            )
            emit(event, result=result, language=language)
            return 0
        became_managed = observe_initial_name(state, current, event)
        if not write_state(state_file, state):
            emit(event, result=RESULT_IO_ERROR, language=language)
            return 0
        if became_managed and event == "Stop":
            result = apply_requested_title(
                state,
                database,
                state_file,
                title_file,
                worktree_prefix,
                fallback_title,
                language,
            )
            emit(event, result=result, language=language)
            return 0
        if event == "Stop":
            emit(event, language=language)
            return 0
        emit(
            event,
            context_for_state(state, "ok", current, title_file, language),
            language=language,
        )
        return 0

    if state.get("phase") == PHASE_SOURCE_UNCONFIRMED:
        current_result, current = read_thread_name(database, session_id)
        if event == "Stop":
            emit(event, language=language)
        else:
            emit(
                event,
                context_for_state(
                    state, PHASE_SOURCE_UNCONFIRMED, current, title_file, language
                ),
                language=language,
            )
        return 0

    if event in {"Stop", "PostToolUse"}:
        result = apply_requested_title(
            state,
            database,
            state_file,
            title_file,
            worktree_prefix,
            fallback_title,
            language,
        )
        emit(event, result=result, language=language)
        return 0

    prompt_apply_result = "ok"
    if (
        event == "UserPromptSubmit"
        and state.get("phase") == PHASE_MANAGED
        and title_file.exists()
    ):
        prompt_apply_result = apply_requested_title(
            state,
            database,
            state_file,
            title_file,
            worktree_prefix,
            fallback_title,
            language,
        )

    current_result, current = read_thread_name(database, session_id)
    if current_result == "ok" and current != state.get("last_applied_name"):
        reconcile_result = reconcile_visible_title(state, state_file, current)
        if reconcile_result == RESULT_IO_ERROR:
            emit(
                event,
                context_for_state(None, RESULT_IO_ERROR, current, title_file, language),
                RESULT_IO_ERROR,
                language,
            )
            return 0
        elif reconcile_result != "ok":
            emit(
                event,
                context_for_state(
                    state, RESULT_NAME_CONFLICT, current, title_file, language
                ),
                language=language,
            )
            return 0

    current_for_context = current if current_result == "ok" else ""
    result_for_context = current_result if current_result != "ok" else prompt_apply_result
    context = context_for_state(
        state, result_for_context, current_for_context, title_file, language
    )
    emit(event, context, result_for_context, language)
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0

    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "")
    if event not in {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}:
        return 0
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        return 0

    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    state_dir = home / "session-titles"
    language = read_language(state_dir / "config.json")
    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        emit(event, result=RESULT_IO_ERROR, language=language)
        return 0

    lock_file = state_dir / f".{session_id}.lock"
    lock_handle = acquire_session_lock(lock_file)
    if lock_handle is None:
        title_file = state_dir / f"{session_id}.txt"
        emit(
            event,
            context_for_state(None, RESULT_IO_ERROR, "", title_file, language),
            RESULT_IO_ERROR,
            language,
        )
        return 0
    try:
        return run_session_transaction(payload)
    finally:
        release_session_lock(lock_handle)


if __name__ == "__main__":
    raise SystemExit(main())
