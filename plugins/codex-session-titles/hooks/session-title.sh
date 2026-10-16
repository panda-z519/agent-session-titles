#!/bin/bash
# 从插件安装目录运行标题同步逻辑。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
exec python3 "$HERE/session-title.py"
