#!/usr/bin/env python3
"""通过 Claude Code 官方会话钩子安全同步三段式标题。"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


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
INITIAL_STATUSES = {"zh": "摸底", "en": "Scoping"}
INITIAL_PROGRESS = {"zh": "分析首条请求", "en": "Reviewing first request"}
GENERIC_INITIAL_REQUEST = {"zh": "处理首条请求", "en": "Handle first request"}
PART_NAMES = {
    "zh": ("状态", "初始需求", "当前进展"),
    "en": ("status", "initial request", "current progress"),
}
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
WORKTREE_PREFIX_PATTERN = re.compile(r"^(?:(?:\[工作树\]|\[worktree\])\s*)+")
MARKDOWN_PREFIX_PATTERN = re.compile(r"^(?:#{1,6}|[-*>])\s*")
SENSITIVE_PROMPT_PATTERN = re.compile(
    r"(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|"
    r"secret|authorization\s*:|bearer\s+[A-Za-z0-9._-]+|sk-[A-Za-z0-9_-]{8,}|"
    r"密码|密钥|令牌|-----BEGIN [A-Z ]+PRIVATE KEY-----)",
    re.IGNORECASE,
)
PRIVATE_LOCATION_PATTERN = re.compile(
    r"(?:https?://|/Users/|/home/|[A-Za-z]:\\)", re.IGNORECASE
)
MAX_TITLE_LENGTH = 100
INITIAL_REQUEST_LIMITS = {"zh": 28, "en": 44}
STATE_VERSION = 2
SESSION_LOCK_TIMEOUT_SECONDS = 2.0
SESSION_LOCK_RETRY_SECONDS = 0.05


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


@contextmanager
def acquire_session_lock(path: Path) -> Iterator[bool]:
    """在有限等待内串行化同一会话的状态读写。"""
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield False
        return

    acquired = False
    try:
        deadline = time.monotonic() + SESSION_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(SESSION_LOCK_RETRY_SECONDS)
            except OSError:
                break
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def read_language(directory: Path) -> str:
    try:
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return DEFAULT_LANGUAGE
    if not isinstance(config, dict):
        return DEFAULT_LANGUAGE
    language = config.get("language")
    return language if language in VALID_STATUSES else DEFAULT_LANGUAGE


def strip_worktree_prefix(value: str) -> str:
    return WORKTREE_PREFIX_PATTERN.sub("", value).strip()


def parse_title(value: str, language: str) -> list[str] | None:
    if CONTROL_CHARACTER_PATTERN.search(value):
        return None
    cleaned = value.strip()
    core = strip_worktree_prefix(cleaned)
    parts = [part.strip() for part in core.split(TITLE_SEPARATORS[language])]
    if len(parts) != 3 or any(not part for part in parts) or len(core) > MAX_TITLE_LENGTH:
        return None
    return parts


def format_title(parts: list[str], language: str, prefix: str = "") -> str:
    return f"{prefix}{TITLE_SEPARATORS[language].join(parts)}"


def read_requested_title(path: Path, language: str) -> tuple[str, list[str] | None]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None
    except (OSError, UnicodeError):
        return "error", None

    lines = content.splitlines()
    if len(lines) != 1:
        return "invalid", None
    parts = parse_title(lines[0], language)
    return ("ok", parts) if parts is not None else ("invalid", None)


def read_applied_title(path: Path) -> tuple[str, str]:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "missing", ""
    except (OSError, UnicodeError):
        return "error", ""
    return ("ok", value) if value else ("error", "")


def write_text_atomically(path: Path, value: str) -> bool:
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
            temporary_file.write(value)
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


def valid_parts(value: Any, language: str) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(part, str) and part.strip() for part in value)
        and len(TITLE_SEPARATORS[language].join(value)) <= MAX_TITLE_LENGTH
    )


def valid_state(state: Any, session_id: str) -> bool:
    if not isinstance(state, dict):
        return False
    language = state.get("language")
    if (
        state.get("version") != STATE_VERSION
        or state.get("session_id") != session_id
        or language not in VALID_STATUSES
        or not isinstance(state.get("whole_name_protected"), bool)
        or not isinstance(state.get("middle_provisional"), bool)
        or not isinstance(state.get("applied_sync_pending"), bool)
    ):
        return False
    locked = state.get("locked_parts")
    if not (
        isinstance(locked, list)
        and len(locked) == 3
        and all(isinstance(item, bool) for item in locked)
    ):
        return False
    for key in ("confirmed_parts", "effective_parts"):
        value = state.get(key)
        if value is not None and not valid_parts(value, language):
            return False
    pending = state.get("pending_request")
    return pending is None or (
        isinstance(pending, dict)
        and valid_parts(pending.get("parts"), language)
        and isinstance(pending.get("finalize_middle", False), bool)
    )


def load_state(path: Path, session_id: str) -> tuple[str, dict[str, Any] | None]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "missing", None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "error", None
    return ("ok", state) if valid_state(state, session_id) else ("error", None)


def write_state(path: Path, state: dict[str, Any]) -> bool:
    content = json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n"
    return write_text_atomically(path, content)


def new_state(
    session_id: str,
    language: str,
    *,
    middle_provisional: bool = False,
) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "session_id": session_id,
        "language": language,
        "confirmed_parts": None,
        "effective_parts": None,
        "locked_parts": [False, not middle_provisional, False],
        "pending_request": None,
        "middle_provisional": middle_provisional,
        "applied_sync_pending": False,
        "whole_name_protected": False,
        "protection_reason": None,
    }


def protect_whole_name(state: dict[str, Any], reason: str) -> None:
    state["confirmed_parts"] = None
    state["effective_parts"] = None
    state["pending_request"] = None
    state["locked_parts"] = [True, True, True]
    state["middle_provisional"] = False
    state["applied_sync_pending"] = False
    state["whole_name_protected"] = True
    state["protection_reason"] = reason


def current_custom_title(payload: dict[str, Any]) -> str:
    value = payload.get("session_title")
    return value.strip() if isinstance(value, str) else ""


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


def extract_initial_request(prompt_value: Any, language: str) -> str:
    fallback = GENERIC_INITIAL_REQUEST[language]
    if not isinstance(prompt_value, str):
        return fallback
    if (
        "<pasted_content" in prompt_value
        or SENSITIVE_PROMPT_PATTERN.search(prompt_value)
        or PRIVATE_LOCATION_PATTERN.search(prompt_value)
    ):
        return fallback

    line = next((item.strip() for item in prompt_value.splitlines() if item.strip()), "")
    if not line or line.startswith("<") or "```" in line:
        return fallback
    line = MARKDOWN_PREFIX_PATTERN.sub("", line)
    line = line.replace("｜", " ").replace("|", " ")
    line = CONTROL_CHARACTER_PATTERN.sub(" ", line)
    line = re.sub(r"\s+", " ", line).strip(" \t\"'`“”‘’。.!！?？:：;；")
    if language == "zh":
        line = re.sub(r"^(?:请帮我|麻烦帮我|帮我|我需要|我想要|请)\s*", "", line)
    else:
        line = re.sub(
            r"^(?:please\s+|could you\s+|can you\s+|i need you to\s+|i want you to\s+)",
            "",
            line,
            flags=re.IGNORECASE,
        )
    if line.casefold() in {"继续", "开始", "continue", "go", "start"}:
        return fallback
    limit = INITIAL_REQUEST_LIMITS[language]
    if len(line) > limit:
        line = line[: limit - 1].rstrip() + "…"
    return line if len(line) >= 2 else fallback


def locked_part_context(state: dict[str, Any], language: str) -> str:
    locked_names = [
        PART_NAMES[language][index]
        for index, locked in enumerate(state["locked_parts"])
        if locked
    ]
    if not locked_names:
        return "No parts are locked yet. " if language == "en" else "暂无锁定字段。"
    if language == "en":
        return "Locked parts: " + ", ".join(locked_names) + ". "
    return "已锁定字段：" + "、".join(locked_names) + "。"


def writing_guidance(
    title_file: Path,
    language: str,
    state: dict[str, Any] | None = None,
) -> str:
    lock_context = locked_part_context(state, language) if state is not None else ""
    provisional = bool(state and state["middle_provisional"])
    if language == "en":
        provisional_context = (
            "The initial-request part is provisional and may be refined once from the "
            "current task, then it becomes fixed. "
            if provisional
            else "Keep the established initial-request part unchanged. "
        )
        return (
            f"Session title file: {title_file}. {lock_context}{provisional_context}When the "
            "phase or progress changes, overwrite this file as early as possible in the turn "
            'with one line in the form "Status | Initial request | Current progress". The '
            "hook applies it on the next UserPromptSubmit. For unlocked automatic status "
            "updates, use Scoping, Planning, Building, Testing, Fixing, Awaiting "
            "confirmation, Completed, or Paused. Preserve locked parts; a manually locked "
            "status may use other words. The hook adds the worktree prefix, and no write is "
            "needed while the title is accurate."
        )
    provisional_context = (
        "初始需求目前只是临时短名，可根据本轮真实任务精炼一次，"
        "随后固定。"
        if provisional
        else "已建立的初始需求保持不变。"
    )
    return (
        f"会话标题文件：{title_file}。{lock_context}{provisional_context}"
        "阶段或进展变化时，在本轮尽早用写入文件的工具覆盖写入一行"
        "「状态｜初始需求｜当前进展」，钩子会在下一次 "
        "UserPromptSubmit 应用。未锁定的自动状态更新从摸底、方案、"
        "开发中、测试、修复、待确认、已完成、搁置中选择。"
        "保留已锁定字段；人工锁定的状态可用其他词。"
        "工作树前缀由钩子添加，"
        "标题仍准确时不要写。"
    )


def protected_context(language: str) -> str:
    if language == "en":
        return (
            "The custom session title has no safe plugin baseline or cannot be parsed as "
            "three same-language parts. Preserve the whole title and do not write the title "
            "file."
        )
    return (
        "当前自定义会话名称没有可安全比较的插件基线，或无法按同语言"
        "三段解析。整名保护，不写标题文件。"
    )


def io_error_context(language: str) -> str:
    if language == "en":
        return "Title synchronization state cannot be read or saved. Preserve the current title."
    return "标题同步状态暂时无法读取或保存，保留当前名称。"


def lock_error_context(language: str) -> str:
    if language == "en":
        return "Title synchronization is busy. Preserve the current title and retry later."
    return "标题同步正忙，保留当前名称，后续事件会重试。"


def invalid_file_context(title_file: Path, language: str) -> str:
    if language == "en":
        return (
            f"The title file is invalid: {title_file}. Use exactly one nonempty, three-part "
            'line in the form "Status | Initial request | Current progress".'
        )
    return (
        f"标题文件内容无效：{title_file}。请只写一行非空三段标题"
        "「状态｜初始需求｜当前进展」。"
    )


def migrate_legacy_applied(
    session_id: str,
    language: str,
    applied_file: Path,
    current: str,
) -> tuple[str, dict[str, Any] | None]:
    result, applied = read_applied_title(applied_file)
    if result != "ok":
        return result, None
    applied_parts = parse_title(applied, language)
    if applied_parts is None:
        state = new_state(session_id, language)
        protect_whole_name(state, "legacy_baseline_unparseable")
        return "ok", state

    state = new_state(session_id, language)
    state["effective_parts"] = applied_parts
    if not current:
        # 旧 .applied 在输出 sessionTitle 前写入，只能视为待确认请求。
        state["pending_request"] = {"parts": applied_parts, "source": "legacy"}
        return "ok", state

    current_parts = parse_title(current, language)
    if current_parts is None:
        protect_whole_name(state, "legacy_current_unparseable")
        return "ok", state
    state["confirmed_parts"] = applied_parts
    for index, current_part in enumerate(current_parts):
        if current_part != applied_parts[index]:
            state["locked_parts"][index] = True
            state["effective_parts"][index] = current_part
    return "ok", state


def resolve_state(
    session_id: str,
    language: str,
    state_file: Path,
    applied_file: Path,
    current: str,
) -> tuple[str, dict[str, Any] | None]:
    result, state = load_state(state_file, session_id)
    if result == "ok" and state is not None:
        if state["language"] != language:
            protect_whole_name(state, "language_changed")
            state["language"] = language
            return ("ok", state) if write_state(state_file, state) else ("error", None)
        return "ok", state
    if result == "error":
        return "error", None

    legacy_result, legacy_state = migrate_legacy_applied(
        session_id, language, applied_file, current
    )
    if legacy_result == "error":
        return "error", None
    if legacy_result == "ok" and legacy_state is not None:
        if not write_state(state_file, legacy_state):
            return "error", None
        return "ok", legacy_state
    return "missing", None


def reconcile_current_title(
    state: dict[str, Any],
    current: str,
    language: str,
) -> bool:
    """将当前可见标题合并进逐段锁定状态，并返回状态是否变化。"""
    if state["whole_name_protected"]:
        return False
    confirmed = state["confirmed_parts"]
    pending = state["pending_request"]
    effective = state["effective_parts"]

    if not current:
        if confirmed is not None:
            protect_whole_name(state, "confirmed_custom_title_missing")
            return True
        return False

    current_parts = parse_title(current, language)
    if current_parts is None:
        protect_whole_name(state, "current_title_unparseable")
        return True
    if effective is None:
        protect_whole_name(state, "custom_title_without_baseline")
        return True

    if pending is not None and current_parts == pending["parts"]:
        if pending.get("finalize_middle", False):
            state["middle_provisional"] = False
            state["locked_parts"][1] = True
        state["confirmed_parts"] = current_parts.copy()
        state["effective_parts"] = current_parts.copy()
        state["pending_request"] = None
        state["applied_sync_pending"] = True
        return True

    if confirmed is None:
        if pending is None:
            protect_whole_name(state, "custom_title_before_initial_confirmation")
            return True
        # 首次输出后用户可能马上改某一段；
        # 按已请求标题比较并保留每段改动。
        for index, current_part in enumerate(current_parts):
            if current_part != pending["parts"][index]:
                state["locked_parts"][index] = True
                if index == 1:
                    state["middle_provisional"] = False
        if (
            pending.get("finalize_middle", False)
            and current_parts[1] == pending["parts"][1]
        ):
            state["middle_provisional"] = False
            state["locked_parts"][1] = True
        state["confirmed_parts"] = current_parts.copy()
        state["effective_parts"] = current_parts.copy()
        state["pending_request"] = None
        state["applied_sync_pending"] = True
        return True

    changed = False
    pending_parts = pending["parts"] if pending is not None else None
    for index, current_part in enumerate(current_parts):
        expected_part = effective[index]
        if current_part == expected_part:
            continue
        if pending_parts is not None and current_part == pending_parts[index]:
            # 部分与已请求值一致不视为人工修改；
            # 整名一致前仍不确认应用。
            state["effective_parts"][index] = current_part
            if index == 1 and pending.get("finalize_middle", False):
                state["middle_provisional"] = False
                state["locked_parts"][1] = True
            changed = True
            continue
        state["effective_parts"][index] = current_part
        state["locked_parts"][index] = True
        if index == 1:
            state["middle_provisional"] = False
        changed = True
    return changed


def requested_parts_for_state(
    state: dict[str, Any],
    requested: list[str],
    language: str,
) -> list[str] | None:
    effective = state["effective_parts"]
    if effective is None:
        desired = requested.copy()
    else:
        desired = effective.copy()
        for index in range(3):
            if not state["locked_parts"][index]:
                desired[index] = requested[index]
    if not state["locked_parts"][0] and desired[0] not in VALID_STATUSES[language]:
        return None
    if len(TITLE_SEPARATORS[language].join(desired)) > MAX_TITLE_LENGTH:
        return None
    return desired


def persist_reconciled_state(
    state_file: Path,
    applied_file: Path,
    state: dict[str, Any],
    changed: bool,
) -> tuple[bool, bool]:
    if changed and not write_state(state_file, state):
        return False, False
    if not state["applied_sync_pending"]:
        return True, True
    confirmed_parts = state["confirmed_parts"]
    if confirmed_parts is None:
        return True, False
    confirmed_title = format_title(confirmed_parts, state["language"])
    if not write_text_atomically(applied_file, confirmed_title + "\n"):
        return True, False
    state["applied_sync_pending"] = False
    # 此处状态回写失败也可安全重试；.applied 重复覆盖是幂等的。
    write_state(state_file, state)
    return True, True


def process_session_start(
    payload: dict[str, Any],
    session_id: str,
    title_file: Path,
    state_file: Path,
    applied_file: Path,
    language: str,
) -> None:
    current = current_custom_title(payload)
    result, state = resolve_state(
        session_id, language, state_file, applied_file, current
    )
    if result == "error":
        emit("SessionStart", context=io_error_context(language))
        return
    if state is None:
        if current:
            state = new_state(session_id, language)
            protect_whole_name(state, "custom_title_without_baseline")
            if not write_state(state_file, state):
                emit("SessionStart", context=io_error_context(language))
                return
            emit("SessionStart", context=protected_context(language))
            return
        emit("SessionStart", context=writing_guidance(title_file, language))
        return

    changed = reconcile_current_title(state, current, language)
    saved, legacy_saved = persist_reconciled_state(
        state_file, applied_file, state, changed
    )
    if not saved:
        emit("SessionStart", context=io_error_context(language))
        return
    if state["whole_name_protected"]:
        emit("SessionStart", context=protected_context(language))
        return
    context = writing_guidance(title_file, language, state)
    if not legacy_saved:
        context += (
            " Legacy .applied compatibility data will be retried later."
            if language == "en"
            else "旧版 .applied 兼容基线暂未写入，后续事件会重试。"
        )
    emit("SessionStart", context=context)


def process_user_prompt(
    payload: dict[str, Any],
    session_id: str,
    title_file: Path,
    state_file: Path,
    applied_file: Path,
    language: str,
) -> None:
    current = current_custom_title(payload)
    result, state = resolve_state(
        session_id, language, state_file, applied_file, current
    )
    if result == "error":
        emit("UserPromptSubmit", context=io_error_context(language))
        return
    if state is None and current:
        state = new_state(session_id, language)
        protect_whole_name(state, "custom_title_without_baseline")
        if not write_state(state_file, state):
            emit("UserPromptSubmit", context=io_error_context(language))
            return
        emit("UserPromptSubmit", context=protected_context(language))
        return
    if state is not None:
        changed = reconcile_current_title(state, current, language)
        saved, legacy_saved = persist_reconciled_state(
            state_file, applied_file, state, changed
        )
        if not saved:
            emit("UserPromptSubmit", context=io_error_context(language))
            return
        if state["whole_name_protected"]:
            emit("UserPromptSubmit", context=protected_context(language))
            return
    else:
        legacy_saved = True

    requested_result, requested = read_requested_title(title_file, language)
    title_file_requested = requested_result == "ok"
    if requested_result == "error":
        emit("UserPromptSubmit", context=io_error_context(language))
        return
    if requested_result == "invalid":
        emit("UserPromptSubmit", context=invalid_file_context(title_file, language))
        return
    if requested is None:
        if state is None:
            requested = [
                INITIAL_STATUSES[language],
                extract_initial_request(payload.get("prompt"), language),
                INITIAL_PROGRESS[language],
            ]
        elif state["pending_request"] is not None:
            requested = state["pending_request"]["parts"].copy()
        else:
            emit(
                "UserPromptSubmit",
                context=writing_guidance(title_file, language, state),
            )
            return

    newly_initialized = state is None
    if newly_initialized:
        state = new_state(
            session_id,
            language,
            middle_provisional=not title_file_requested,
        )
        desired = requested.copy()
        if desired[0] not in VALID_STATUSES[language]:
            emit("UserPromptSubmit", context=invalid_file_context(title_file, language))
            return
        state["effective_parts"] = desired.copy()
    else:
        desired = requested_parts_for_state(state, requested, language)
        if desired is None:
            emit("UserPromptSubmit", context=invalid_file_context(title_file, language))
            return

    pending = state["pending_request"]
    pending_middle_finalization = bool(
        pending and pending.get("finalize_middle", False)
    )
    finalize_middle_requested = state["middle_provisional"] and (
        title_file_requested or pending_middle_finalization
    )

    if (
        not newly_initialized
        and state["pending_request"] is None
        and desired == state["effective_parts"]
    ):
        if finalize_middle_requested:
            state["middle_provisional"] = False
            state["locked_parts"][1] = True
            if not write_state(state_file, state):
                emit("UserPromptSubmit", context=io_error_context(language))
                return
        context = writing_guidance(title_file, language, state)
        if not legacy_saved:
            context += (
                " Legacy .applied compatibility data will be retried later."
                if language == "en"
                else "旧版 .applied 兼容基线暂未写入，后续事件会重试。"
            )
        emit("UserPromptSubmit", context=context)
        return

    request_source = pending.get("source", "initial_prompt") if pending else "initial_prompt"
    if title_file_requested:
        request_source = "title_file"
    state["pending_request"] = {
        "parts": desired.copy(),
        "source": request_source,
        "finalize_middle": finalize_middle_requested,
    }
    if not write_state(state_file, state):
        emit("UserPromptSubmit", context=io_error_context(language))
        return
    desired_title = format_title(
        desired, language, worktree_prefix(payload.get("cwd"), language)
    )
    emit(
        "UserPromptSubmit",
        title=desired_title,
        context=writing_guidance(title_file, language, state),
    )


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
    if not ensure_title_directory(directory):
        emit(event, context="会话标题目录暂时无法创建，保留现有名称。")
        return 0
    language = read_language(directory)
    title_file = directory / f"{session_id}.txt"
    state_file = directory / f"{session_id}.state.json"
    applied_file = directory / f"{session_id}.applied"
    lock_file = directory / f"{session_id}.lock"

    with acquire_session_lock(lock_file) as acquired:
        if not acquired:
            emit(event, context=lock_error_context(language))
            return 0
        if event == "SessionStart":
            process_session_start(
                payload,
                session_id,
                title_file,
                state_file,
                applied_file,
                language,
            )
        else:
            process_user_prompt(
                payload,
                session_id,
                title_file,
                state_file,
                applied_file,
                language,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
