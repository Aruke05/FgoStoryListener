#!/usr/bin/env bash
set -euo pipefail

project=$(cd -- "$(dirname -- "$0")" && pwd)
cd -- "$project"

python=${FGO_BUILD_PYTHON:-python3}
if [[ -x .venv/bin/python && -z ${FGO_BUILD_PYTHON:-} ]]; then
  python=.venv/bin/python
fi

"$python" - <<'PY'
import platform
import sys
import tkinter

if sys.platform != "darwin" or platform.machine() != "arm64":
    raise SystemExit("请在 Apple Silicon macOS 上使用 arm64 Python 构建。")
if sys.version_info < (3, 11):
    raise SystemExit("构建需要 Python 3.11 或更新版本。")
PY

server=runtime/frida-server-17.17.0-android-arm64
if [[ ! -s "$server" ]] || head -c 4 "$server" | grep -q '^vers'; then
  git lfs pull --include="$server" --exclude=""
fi
if ! head -c 4 "$server" | grep -q $'^\x7fELF'; then
  printf '缺少可用的 ARM64 Frida Server：%s\n' "$server" >&2
  exit 1
fi

"$python" -m PyInstaller --noconfirm --clean \
  --distpath "$project/dist-macos" --workpath "$project/build/macos" \
  "$project/FgoStoryListener.spec"
codesign --verify --deep --strict "$project/dist-macos/FgoStoryListener.app"
printf 'Built %s\n' "$project/dist-macos/FgoStoryListener.app"
