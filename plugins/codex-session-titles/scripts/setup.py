#!/usr/bin/env python3
"""为插件准备会话标题状态目录和 Codex 沙箱写入权限。"""
from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


class SetupError(RuntimeError):
    """安装前置检查或安全写入失败。"""


# 备份名随插件版本变化，同一版本只备份一次
PLUGIN_VERSION = json.loads(
    (Path(__file__).resolve().parent.parent / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
)["version"]
DATABASE_BACKUP_NAME = f"state_5.before-codex-session-titles-{PLUGIN_VERSION}.sqlite"
DEFAULT_LANGUAGE = "zh"
SUPPORTED_LANGUAGES = {"zh", "en"}
REQUIRED_THREAD_COLUMNS = {"id", "name", "archived"}


def iter_hook_commands(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str):
            yield command
        for nested in value.values():
            yield from iter_hook_commands(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_hook_commands(nested)


def command_references_path(command: str, target: Path) -> bool:
    expanded_command = os.path.expandvars(os.path.expanduser(command))
    if str(target) in expanded_command:
        return True
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    for token in tokens:
        expanded = os.path.expandvars(os.path.expanduser(token))
        try:
            if Path(expanded).resolve() == target:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def reject_legacy_user_hook(codex_home: Path) -> None:
    hooks_file = codex_home / "hooks.json"
    if not hooks_file.exists():
        return
    try:
        hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError(f"无法安全检查旧版用户级钩子 {hooks_file}：{exc}") from exc

    legacy_script = (codex_home / "hooks" / "session-title.sh").resolve()
    legacy_hook_enabled = any(
        command_references_path(command, legacy_script)
        for command in iter_hook_commands(hooks)
    )
    if legacy_hook_enabled:
        raise SetupError(
            f"检测到 {hooks_file} 仍指向旧版用户级钩子 {legacy_script}。"
            "请先手动停用该钩子再运行插件设置，避免同一事件执行两次；"
            "设置程序不会自动删除它。"
        )


def read_config(config_file: Path) -> tuple[str, dict[str, Any]]:
    try:
        text = config_file.read_text(encoding="utf-8") if config_file.exists() else ""
    except (OSError, UnicodeError) as exc:
        raise SetupError(f"无法读取 {config_file}：{exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"{config_file} 不是合法 TOML，未作修改：{exc}") from exc
    return text, data


def find_array_end(text: str, start: int) -> int:
    depth = 0
    quote = ""
    escaped = False
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {'"', "'"}:
            quote = char
        elif char == "#":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise SetupError(
        "无法定位 sandbox_workspace_write.writable_roots 数组结尾，未作修改。"
    )


def replace_array_value(text: str, assignment: re.Match[str], roots: list[str]) -> str:
    equals = text.find("=", assignment.start(), assignment.end())
    value_start = equals + 1
    while value_start < len(text) and text[value_start] in " \t":
        value_start += 1
    if value_start >= len(text) or text[value_start] != "[":
        raise SetupError(
            "sandbox_workspace_write.writable_roots 必须直接使用 TOML 数组，未作修改。"
        )
    value_end = find_array_end(text, value_start)
    replacement = json.dumps(roots, ensure_ascii=False)
    return text[:value_start] + replacement + text[value_end:]


def update_config_text(
    text: str,
    data: dict[str, Any],
    writable_root: str,
) -> tuple[str, bool]:
    sandbox = data.get("sandbox_workspace_write", {})
    if not isinstance(sandbox, dict):
        raise SetupError("sandbox_workspace_write 必须是 TOML 表，未作修改。")
    roots = sandbox.get("writable_roots", [])
    if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
        raise SetupError(
            "sandbox_workspace_write.writable_roots 必须是字符串数组，未作修改。"
        )
    if writable_root in roots:
        return text, False

    merged_roots = [*roots, writable_root]
    table_pattern = re.compile(
        r"(?m)^[ \t]*\[[ \t]*(?:sandbox_workspace_write|\"sandbox_workspace_write\"|"
        r"'sandbox_workspace_write')[ \t]*\][ \t]*(?:#.*)?(?:\r?\n|$)"
    )
    key_pattern = re.compile(
        r"(?m)^[ \t]*(?:writable_roots|\"writable_roots\"|'writable_roots')[ \t]*="
    )
    dotted_key_pattern = re.compile(
        r"(?m)^[ \t]*(?:sandbox_workspace_write|\"sandbox_workspace_write\"|"
        r"'sandbox_workspace_write')[ \t]*\.[ \t]*"
        r"(?:writable_roots|\"writable_roots\"|'writable_roots')[ \t]*="
    )

    table_match = table_pattern.search(text)
    if table_match:
        next_table = re.search(r"(?m)^[ \t]*\[", text[table_match.end():])
        section_end = (
            table_match.end() + next_table.start() if next_table else len(text)
        )
        key_match = key_pattern.search(text, table_match.end(), section_end)
        if key_match:
            updated_text = replace_array_value(text, key_match, merged_roots)
        else:
            assignment = f"writable_roots = {json.dumps([writable_root], ensure_ascii=False)}\n"
            updated_text = text[:table_match.end()] + assignment + text[table_match.end():]
    else:
        dotted_match = dotted_key_pattern.search(text)
        if dotted_match:
            updated_text = replace_array_value(text, dotted_match, merged_roots)
        elif "sandbox_workspace_write" in data:
            inline_pattern = re.compile(
                r"(?m)^[ \t]*(?:sandbox_workspace_write|\"sandbox_workspace_write\"|"
                r"'sandbox_workspace_write')[ \t]*="
            )
            if inline_pattern.search(text):
                raise SetupError(
                    "无法安全扩展内联 sandbox_workspace_write 表，未作修改。"
                )
            first_table = re.search(r"(?m)^[ \t]*\[", text)
            insert_at = first_table.start() if first_table else 0
            assignment = (
                "sandbox_workspace_write.writable_roots = "
                f"{json.dumps([writable_root], ensure_ascii=False)}\n"
            )
            updated_text = text[:insert_at] + assignment + text[insert_at:]
        else:
            separator = (
                ""
                if not text or text.endswith("\n\n")
                else ("\n" if text.endswith("\n") else "\n\n")
            )
            updated_text = (
                text
                + separator
                + "[sandbox_workspace_write]\n"
                + f"writable_roots = {json.dumps([writable_root], ensure_ascii=False)}\n"
            )

    try:
        updated_data = tomllib.loads(updated_text)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"更新后的 config.toml 校验失败，未作修改：{exc}") from exc
    expected_data = copy.deepcopy(data)
    expected_sandbox = expected_data.setdefault("sandbox_workspace_write", {})
    expected_sandbox["writable_roots"] = merged_roots
    if updated_data != expected_data:
        raise SetupError("config.toml 更新会改变无关配置，未作修改。")
    return updated_text, True


def protect_state_database(codex_home: Path) -> None:
    database = codex_home / "state_5.sqlite"
    if not database.exists():
        print(f"状态库尚未创建，跳过备份：{database}")
        return

    backup_dir = codex_home / "backups"
    backup = backup_dir / DATABASE_BACKUP_NAME
    temp_path: Path | None = None
    source: sqlite3.Connection | None = None
    snapshot: sqlite3.Connection | None = None
    try:
        database_uri = f"{database.resolve().as_uri()}?mode=ro"
        source = sqlite3.connect(database_uri, uri=True, timeout=10)
        source.execute("PRAGMA busy_timeout = 10000")
        columns = {row[1] for row in source.execute("PRAGMA table_info(threads)")}
        missing_columns = REQUIRED_THREAD_COLUMNS - columns
        if missing_columns:
            missing = "、".join(sorted(missing_columns))
            raise SetupError(f"状态库 threads 表缺少必要列：{missing}；未修改配置。")

        if backup.exists():
            print(f"同版本状态库备份已存在，跳过重复备份：{backup}")
            return

        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{DATABASE_BACKUP_NAME}.",
            suffix=".tmp",
            dir=backup_dir,
        )
        os.close(file_descriptor)
        temp_path = Path(temp_name)
        snapshot = sqlite3.connect(temp_path, timeout=10)
        source.backup(snapshot)
        integrity_rows = snapshot.execute("PRAGMA integrity_check").fetchall()
        if integrity_rows != [("ok",)]:
            raise SetupError("状态库快照完整性检查失败；未修改配置。")
        snapshot.close()
        snapshot = None
        source.close()
        source = None

        with temp_path.open("rb") as snapshot_file:
            os.fsync(snapshot_file.fileno())
        try:
            os.link(temp_path, backup)
        except FileExistsError:
            print(f"同版本状态库备份已存在，跳过重复备份：{backup}")
        else:
            print(f"已创建状态库一致性备份：{backup}")
    except SetupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise SetupError(f"状态库结构检查或一致性备份失败；未修改配置：{exc}") from exc
    finally:
        if snapshot is not None:
            snapshot.close()
        if source is not None:
            source.close()
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def next_backup_path(config_file: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    candidate = config_file.with_name(f"{config_file.name}.bak.{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = config_file.with_name(f"{config_file.name}.bak.{stamp}.{suffix}")
        suffix += 1
    return candidate


def atomic_write(config_file: Path, text: str) -> None:
    temp_path: Path | None = None
    source_mode = config_file.stat().st_mode & 0o777 if config_file.exists() else 0o600
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_file.parent,
            prefix=f".{config_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.chmod(source_mode)
        os.replace(temp_path, config_file)
    except OSError as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise SetupError(f"无法安全写入 {config_file}：{exc}") from exc


def parse_language_argument(arguments: list[str]) -> str | None:
    if not arguments:
        return None
    if len(arguments) == 2 and arguments[0] == "--lang":
        language = arguments[1]
        if language in SUPPORTED_LANGUAGES:
            return language
    raise SetupError("--lang 只能指定一次，取值为 zh 或 en。")


def read_language_config(config_file: Path) -> tuple[str, dict[str, Any], bool]:
    try:
        text = config_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULT_LANGUAGE, {}, False
    except (OSError, UnicodeError) as exc:
        raise SetupError(f"无法安全读取语言配置 {config_file}，未作修改：{exc}") from exc
    try:
        config = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{config_file} 不是合法 JSON，未作修改：{exc}") from exc
    if not isinstance(config, dict):
        raise SetupError(f"{config_file} 的顶层结构必须是 JSON 对象，未作修改。")
    language = config.get("language")
    if language is None:
        return DEFAULT_LANGUAGE, config, False
    if language not in SUPPORTED_LANGUAGES:
        raise SetupError(f"{config_file} 的 language 必须是 zh 或 en，未作修改。")
    return language, config, True


def write_language_config(config_file: Path, config: dict[str, Any], language: str) -> None:
    updated = dict(config)
    updated["language"] = language
    atomic_write(
        config_file,
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
    )


def run(requested_language: str | None = None) -> int:
    configured_home = os.environ.get("CODEX_HOME")
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    codex_home = codex_home.resolve()
    config_file = codex_home / "config.toml"
    state_dir = codex_home / "session-titles"
    language_file = state_dir / "config.json"

    reject_legacy_user_hook(codex_home)
    config_text, config_data = read_config(config_file)
    updated_text, changed = update_config_text(config_text, config_data, str(state_dir))
    stored_language, language_config, language_config_valid = read_language_config(
        language_file
    )
    language = requested_language or stored_language
    protect_state_database(codex_home)

    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_dir.chmod(0o700)
    except OSError as exc:
        raise SetupError(f"无法创建权限为 0700 的状态目录 {state_dir}：{exc}") from exc

    if changed:
        if config_file.exists():
            backup = next_backup_path(config_file)
            try:
                shutil.copy2(config_file, backup)
            except OSError as exc:
                raise SetupError(f"无法备份 {config_file}：{exc}") from exc
            print(f"已备份配置：{backup}")
        atomic_write(config_file, updated_text)
        print(f"已更新沙箱可写目录：{state_dir}")
    else:
        print(f"沙箱可写目录已存在：{state_dir}")
    if not language_config_valid or language != stored_language:
        write_language_config(language_file, language_config, language)
        print(f"标题语言已设置为：{language}")
    else:
        print(f"标题语言保持为：{language}")
    print(f"状态目录已准备（0700）：{state_dir}")
    return 0


def main() -> int:
    try:
        return run(parse_language_argument(sys.argv[1:]))
    except SetupError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
