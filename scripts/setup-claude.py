#!/usr/bin/env python3
"""为 Claude Code 插件准备用户级目录写入权限。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


REQUIRED_PERMISSION = "Edit(~/.cc-session-titles/**)"


class SetupError(RuntimeError):
    """Claude Code 用户配置无法安全更新。"""


def read_settings(settings_file: Path) -> dict[str, Any]:
    if not settings_file.exists():
        return {}
    try:
        text = settings_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SetupError(f"无法读取 {settings_file}：{exc}") from exc
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{settings_file} 不是合法 JSON，未作修改：{exc}") from exc
    if not isinstance(settings, dict):
        raise SetupError(f"{settings_file} 的顶层结构必须是 JSON 对象，未作修改。")
    return settings


def add_required_permission(settings: dict[str, Any]) -> bool:
    if "permissions" not in settings:
        permissions = {}
        settings["permissions"] = permissions
    else:
        permissions = settings["permissions"]
    if not isinstance(permissions, dict):
        raise SetupError("settings.json 的 permissions 必须是 JSON 对象，未作修改。")

    if "allow" not in permissions:
        allowed = []
        permissions["allow"] = allowed
    else:
        allowed = permissions["allow"]
    if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
        raise SetupError(
            "settings.json 的 permissions.allow 必须是字符串数组，未作修改。"
        )

    if REQUIRED_PERMISSION in allowed:
        return False
    allowed.append(REQUIRED_PERMISSION)
    return True


def next_backup_path(settings_file: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    candidate = settings_file.with_name(f"{settings_file.name}.bak.{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = settings_file.with_name(
            f"{settings_file.name}.bak.{stamp}.{suffix}"
        )
        suffix += 1
    return candidate


def atomic_write(settings_file: Path, settings: dict[str, Any]) -> None:
    temp_path: Path | None = None
    source_mode = settings_file.stat().st_mode & 0o777 if settings_file.exists() else 0o600
    text = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=settings_file.parent,
            prefix=f".{settings_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.chmod(source_mode)
        os.replace(temp_path, settings_file)
    except OSError as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise SetupError(f"无法安全写入 {settings_file}：{exc}") from exc


def run() -> int:
    configured_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(configured_dir).expanduser() if configured_dir else Path.home() / ".claude"
    config_dir = config_dir.resolve()
    settings_file = config_dir / "settings.json"

    settings = read_settings(settings_file)
    changed = add_required_permission(settings)
    if not changed:
        print(f"Claude 权限已存在：{REQUIRED_PERMISSION}")
        return 0

    try:
        config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise SetupError(f"无法创建 Claude 配置目录 {config_dir}：{exc}") from exc

    if settings_file.exists():
        backup = next_backup_path(settings_file)
        try:
            shutil.copy2(settings_file, backup)
        except OSError as exc:
            raise SetupError(f"无法备份 {settings_file}：{exc}") from exc
        print(f"已备份 Claude 配置：{backup}")

    atomic_write(settings_file, settings)
    print(f"已加入 Claude 用户权限：{REQUIRED_PERMISSION}")
    return 0


def main() -> int:
    try:
        return run()
    except SetupError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
