#!/usr/bin/env python3
"""在 Codex 生命周期钩子中观察初始标题并同步状态文件。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

VALID_STATUSES = {"摸底", "方案", "开发中", "测试", "修复", "待确认", "已完成", "搁置"}
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


def project_context(cwd: str) -> tuple[bool, str, str, str]:
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
            worktree_prefix = "[工作树] "
        project = common_path.parent.name
        branch = subprocess.check_output(
            ["git", "-C", cwd, "branch", "--show-current"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip() or "无分支"
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


def compare_and_set_thread_name(
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
        "pending_update": None,
        "last_result": result if result != "ok" else RESULT_INITIAL_TITLE_PENDING,
        "last_error": None,
    }


def managed_state(session_id: str, name: str, result: str) -> dict[str, Any]:
    return {
        "version": 1,
        "session_id": session_id,
        "phase": PHASE_MANAGED,
        "candidate_name": name,
        "initial_observations": 0,
        "empty_stop_count": 0,
        "last_applied_name": name,
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


def format_requested_title(value: str, worktree_prefix: str) -> str:
    title = re.sub(r"^\[工作树\]\s*", "", value).strip()
    parts = [part.strip() for part in title.split("｜")]
    if len(parts) != 3 or any(not part for part in parts):
        return ""
    status, initial_request, progress = parts
    if status not in VALID_STATUSES:
        return ""
    return f"{worktree_prefix}{status}｜{initial_request}｜{progress}"[:60]


def finish_pending_update(
    state: dict[str, Any],
    state_file: Path,
    title_file: Path,
    pending: dict[str, str],
) -> str:
    state["phase"] = PHASE_MANAGED
    state["last_applied_name"] = pending["desired_name"]
    state["candidate_name"] = pending["desired_name"]
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
        state["phase"] = PHASE_SOURCE_UNCONFIRMED
        state["pending_update"] = None
        state["last_result"] = RESULT_NAME_CONFLICT
        state["last_error"] = None
        write_state(state_file, state)
        return RESULT_NAME_CONFLICT
    result = compare_and_set_thread_name(database, state["session_id"], expected, desired)
    if result == RESULT_UPDATED:
        return finish_pending_update(state, state_file, title_file, pending)
    state["last_result"] = result
    state["last_error"] = result
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
    if not isinstance(expected, str) or current != expected:
        state["phase"] = PHASE_SOURCE_UNCONFIRMED
        state["last_result"] = RESULT_NAME_CONFLICT
        state["observed_name"] = current
        state["last_error"] = None
        write_state(state_file, state)
        return RESULT_NAME_CONFLICT

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
    desired = fallback_title if use_fallback else format_requested_title(file_content, worktree_prefix)
    if not desired:
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
    if result == RESULT_NAME_CONFLICT:
        state["phase"] = PHASE_SOURCE_UNCONFIRMED
        state["pending_update"] = None
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
) -> str:
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
            "首轮实质回复后或主题变化时，"
            "用写入文件的工具写一行「状态｜初始需求｜当前进展」。"
            "中间段概括用户最初要解决的问题并保持不变；末段概括目前进展。"
            "标题仍准确时不要写。"
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
        "标题仍准确时不要写。"
    )


def emit(event: str, context: str = "", result: str = "ok") -> None:
    if event == "Stop":
        if result == RESULT_IO_ERROR:
            message = "会话标题同步暂时失败，标题文件已保留，后续会重试。"
            print(json.dumps({"systemMessage": message}, ensure_ascii=False))
        else:
            print("{}")
    else:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": context,
            }
        }, ensure_ascii=False))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0

    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id", "")
    if event not in {"SessionStart", "UserPromptSubmit", "Stop"}:
        return 0
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        return 0

    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    state_dir = home / "session-titles"
    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        emit(event, result=RESULT_IO_ERROR)
        return 0

    state_file = state_dir / f"{session_id}.state.json"
    title_file = state_dir / f"{session_id}.txt"
    applied_file = state_dir / f"{session_id}.applied"
    database = home / "state_5.sqlite"
    source = payload.get("source", "")
    has_project, worktree_prefix, project, branch = project_context(payload.get("cwd") or os.getcwd())
    fallback_title = f"{worktree_prefix}{project}·{branch}" if has_project else "摸底｜新会话"

    load_result, state = load_state(state_file)
    if load_result == RESULT_IO_ERROR:
        current_result, current = read_thread_name(database, session_id)
        emit(event, context_for_state(None, RESULT_IO_ERROR, current, title_file), RESULT_IO_ERROR)
        return 0

    if load_result == "missing":
        applied = read_applied(applied_file)
        if applied:
            current_result, current = read_thread_name(database, session_id)
            if current_result == "ok" and current == applied:
                state = managed_state(session_id, applied, "legacy_baseline_migrated")
                if not write_state(state_file, state):
                    emit(event, result=RESULT_IO_ERROR)
                    return 0
                load_result = "ok"
            elif current_result == RESULT_THREAD_NOT_READY:
                state = managed_state(session_id, applied, RESULT_THREAD_NOT_READY)
                if not write_state(state_file, state):
                    emit(event, result=RESULT_IO_ERROR)
                    return 0
                load_result = "ok"
            elif event == "UserPromptSubmit" and current_result == "ok":
                # 用户在恢复的历史会话中发来新消息，才将当前名称作为基线纳入管理。
                state = new_initial_state(
                    session_id, "ok", current, "resumed_after_new_prompt"
                )
                if not write_state(state_file, state):
                    emit(event, result=RESULT_IO_ERROR)
                    return 0
                load_result = "ok"
            else:
                current = current if current_result == "ok" else ""
                emit(event, context_for_state(None, PHASE_SOURCE_UNCONFIRMED, current, title_file))
                return 0
        elif event == "SessionStart" and source == "startup":
            current_result, current = read_thread_name(database, session_id)
            state = new_initial_state(session_id, current_result, current)
            if not write_state(state_file, state):
                emit(event, result=RESULT_IO_ERROR)
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
                emit(event, result=RESULT_IO_ERROR)
                return 0
            load_result = "ok"
        else:
            current_result, current = read_thread_name(database, session_id)
            if current_result != "ok":
                current = ""
            emit(event, context_for_state(None, PHASE_SOURCE_UNCONFIRMED, current, title_file))
            return 0

    if state is None or state.get("session_id") != session_id:
        emit(event, result=RESULT_IO_ERROR)
        return 0

    if state.get("pending_update"):
        result = recover_pending_update(state, database, state_file, title_file)
        if result not in {RESULT_UPDATED, "ok", "unchanged"}:
            if event == "Stop":
                emit(event, result=result)
            else:
                current_result, current = read_thread_name(database, session_id)
                emit(event, context_for_state(state, result, current, title_file), result)
            return 0

    if state.get("phase") == PHASE_INITIALIZING:
        current_result, current = read_thread_name(database, session_id)
        if current_result != "ok":
            state["last_result"] = current_result
            state["last_error"] = current_result
            if not write_state(state_file, state):
                current_result = RESULT_IO_ERROR
            if event == "Stop":
                emit(event, result=current_result)
            else:
                emit(event, context_for_state(state, current_result, "", title_file), current_result)
            return 0
        became_managed = observe_initial_name(state, current, event)
        if not write_state(state_file, state):
            emit(event, result=RESULT_IO_ERROR)
            return 0
        if became_managed and event == "Stop":
            result = apply_requested_title(
                state, database, state_file, title_file, worktree_prefix, fallback_title
            )
            emit(event, result=result)
            return 0
        if event == "Stop":
            emit(event)
            return 0
        emit(event, context_for_state(state, "ok", current, title_file))
        return 0

    if state.get("phase") == PHASE_SOURCE_UNCONFIRMED:
        current_result, current = read_thread_name(database, session_id)
        if event == "Stop":
            emit(event)
        else:
            emit(event, context_for_state(state, PHASE_SOURCE_UNCONFIRMED, current, title_file))
        return 0

    if event == "Stop":
        result = apply_requested_title(
            state, database, state_file, title_file, worktree_prefix, fallback_title
        )
        emit(event, result=result)
        return 0

    current_result, current = read_thread_name(database, session_id)
    if current_result == "ok" and current != state.get("last_applied_name"):
        state["phase"] = PHASE_SOURCE_UNCONFIRMED
        state["last_result"] = RESULT_NAME_CONFLICT
        state["last_error"] = None
        state["observed_name"] = current
        if not write_state(state_file, state):
            emit(event, context_for_state(None, RESULT_IO_ERROR, current, title_file), RESULT_IO_ERROR)
        else:
            emit(event, context_for_state(state, RESULT_NAME_CONFLICT, current, title_file))
        return 0

    current_for_context = current if current_result == "ok" else ""
    result_for_context = current_result if current_result != "ok" else "ok"
    context = context_for_state(state, result_for_context, current_for_context, title_file)
    emit(event, context, result_for_context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
