#!/usr/bin/env python3
"""通过 Claude Code 生命周期钩子同步三段式会话标题。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


VALID_EVENTS = {"SessionStart", "UserPromptSubmit"}
DEFAULT_LANGUAGE = "zh"
VALID_STATUSES = {
    "zh": {
        "摸底",
        "方案",
        "开发中",
        "测试",
        "修复",
        "待确认",
        "已完成",
        "搁置",
    },
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
TITLE_SEPARATORS = {"zh": "｜", "en": " | "}
WORKTREE_PREFIXES = {"zh": "[工作树] ", "en": "[worktree] "}
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
WORKTREE_PREFIX_PATTERN = re.compile(r"^(?:(?:\[工作树\]|\[worktree\])\s*)+")
MAX_TITLE_LENGTH = 100


def emit(event: str, *, context: str = "", title: str = "") -> None:
    output: dict[str, str] = {"hookEventName": event}
    if title:
        output["sessionTitle"] = title
    if context:
        output["additionalContext"] = context
    print(json.dumps({"hookSpecificOutput": output}, ensure_ascii=False))


def title_directory() -> Path:
    configured = os.environ.get("CC_TITLE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".cc-session-titles"


def ensure_title_directory(directory: Path) -> bool:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def read_language(directory: Path) -> str:
    try:
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return DEFAULT_LANGUAGE
    if not isinstance(config, dict):
        return DEFAULT_LANGUAGE
    language = config.get("language")
    return language if language in VALID_STATUSES else DEFAULT_LANGUAGE


def read_applied_title(path: Path) -> tuple[str, str]:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "missing", ""
    except (OSError, UnicodeError):
        return "error", ""
    return ("ok", value) if value else ("error", "")


def read_requested_title(path: Path, language: str) -> tuple[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", ""
    except (OSError, UnicodeError):
        return "error", ""

    lines = content.splitlines()
    first_line = lines[0] if lines else ""
    cleaned = CONTROL_CHARACTER_PATTERN.sub("", first_line).strip()
    cleaned = WORKTREE_PREFIX_PATTERN.sub("", cleaned).strip()
    separator = TITLE_SEPARATORS[language]
    parts = [part.strip() for part in cleaned.split(separator)]
    if (
        len(parts) != 3
        or any(not part for part in parts)
        or parts[0] not in VALID_STATUSES[language]
        or len(cleaned) > MAX_TITLE_LENGTH
    ):
        return "invalid", ""
    return "ok", separator.join(parts)


def resolve_git_path(cwd: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else cwd / path).resolve()


def git_path(cwd: Path, argument: str) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", argument],
            text=True,
            capture_output=True,
            check=True,
            timeout=2,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return resolve_git_path(cwd, value) if value else None


def worktree_prefix(cwd_value: Any, language: str) -> str:
    cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()
    git_directory = git_path(cwd, "--git-dir")
    common_directory = git_path(cwd, "--git-common-dir")
    if git_directory is None or common_directory is None:
        return ""
    return WORKTREE_PREFIXES[language] if git_directory != common_directory else ""


def write_applied_title(path: Path, title: str) -> bool:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(title + "\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        return True
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def writing_guidance(title_file: Path, language: str) -> str:
    if language == "en":
        return (
            f"Session title file: {title_file}. After the first substantive response, "
            "and whenever the phase or topic clearly changes, use the file-writing tool "
            "to overwrite it with one line in the form "
            "\"Status | Initial request | Current progress\". Choose Status from "
            "Scoping, Planning, Building, Testing, Fixing, Awaiting confirmation, "
            "Completed, or Paused. Keep Initial request unchanged once established. "
            "The hook adds the worktree prefix. Do not write when the title is still accurate."
        )
    return (
        f"会话标题文件：{title_file}。"
        "第一次实质回复后，以及阶段或主题明显变化时，"
        "用写入文件的工具覆盖写入一行「状态｜初始需求｜当前进展」。"
        "状态从摸底、方案、开发中、测试、修复、待确认、已完成、"
        "搁置中选择；初始需求确定后保持不变，工作树前缀由钩子添加。"
        "标题仍准确时不要写。"
    )


def source_unconfirmed_context(current: str, applied: str, language: str) -> str:
    if language == "en":
        return (
            f'The current session title "{current or "Untitled"}" differs from the title '
            f'the hook last applied, "{applied}". The title source is unconfirmed. '
            "Keep the current title and do not write the title file."
        )
    return (
        f"当前会话名称「{current or '未命名'}」与钩子上次应用的名称"
        f"「{applied}」不一致，"
        "名称来源未确认。保留现有名称，不写标题文件。"
    )


def process_session_start(
    payload: dict[str, Any],
    title_file: Path,
    applied_file: Path,
    language: str,
) -> None:
    applied_result, applied = read_applied_title(applied_file)
    current = payload.get("session_title")
    current = current.strip() if isinstance(current, str) else ""
    if applied_result == "ok" and current != applied:
        emit(
            "SessionStart",
            context=source_unconfirmed_context(current, applied, language),
        )
        return
    if applied_result == "error":
        emit(
            "SessionStart",
            context=(
                "The title synchronization baseline cannot be read right now. Keep the "
                "current title and do not write the title file during this turn."
                if language == "en"
                else "标题同步基线暂时无法读取，保留现有名称，本轮不要写标题文件。"
            ),
        )
        return
    emit("SessionStart", context=writing_guidance(title_file, language))


def process_user_prompt(
    payload: dict[str, Any],
    title_file: Path,
    applied_file: Path,
    language: str,
) -> None:
    applied_result, applied = read_applied_title(applied_file)
    current = payload.get("session_title")
    current = current.strip() if isinstance(current, str) else ""
    if applied_result == "ok" and current != applied:
        emit(
            "UserPromptSubmit",
            context=source_unconfirmed_context(current, applied, language),
        )
        return
    if applied_result == "error":
        emit(
            "UserPromptSubmit",
            context=(
                "The title synchronization baseline cannot be read right now. Keep the "
                "current title and do not write the title file during this turn."
                if language == "en"
                else "标题同步基线暂时无法读取，保留现有名称，本轮不要写标题文件。"
            ),
        )
        return

    title_result, requested = read_requested_title(title_file, language)
    if title_result == "missing":
        emit("UserPromptSubmit", context=writing_guidance(title_file, language))
        return
    if title_result == "error":
        emit(
            "UserPromptSubmit",
            context=(
                f"The title file cannot be read right now: {title_file}. Keep the current "
                "title; a later prompt will retry."
                if language == "en"
                else f"标题文件暂时无法读取：{title_file}。保留现有名称，后续提示词会重试。"
            ),
        )
        return
    if title_result == "invalid":
        emit(
            "UserPromptSubmit",
            context=(
                f"The title file is invalid: {title_file}. Keep the current title. Change "
                "it to one line in the form \"Status | Initial request | Current progress\" "
                "and use an allowed status."
                if language == "en"
                else f"标题文件内容无效：{title_file}。保留现有名称；请改为一行"
                "「状态｜初始需求｜当前进展」，并使用规定的状态词。"
            ),
        )
        return

    desired = f"{worktree_prefix(payload.get('cwd'), language)}{requested}"
    if applied_result == "ok" and desired == applied:
        emit("UserPromptSubmit", context=writing_guidance(title_file, language))
        return
    if not write_applied_title(applied_file, desired):
        emit(
            "UserPromptSubmit",
            context=(
                "The title synchronization baseline cannot be saved right now. Keep the "
                "current title; a later prompt will retry."
                if language == "en"
                else "标题同步基线暂时无法保存，保留现有名称，后续提示词会重试。"
            ),
        )
        return
    emit("UserPromptSubmit", title=desired)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0

    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")
    if event not in VALID_EVENTS:
        return 0
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        return 0

    directory = title_directory()
    title_file = directory / f"{session_id}.txt"
    applied_file = directory / f"{session_id}.applied"
    if not ensure_title_directory(directory):
        emit(event, context="会话标题目录暂时无法创建，保留现有名称。")
        return 0
    language = read_language(directory)

    if event == "SessionStart":
        process_session_start(payload, title_file, applied_file, language)
    else:
        process_user_prompt(payload, title_file, applied_file, language)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
