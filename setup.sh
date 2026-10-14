#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '需要 Python 3.11 或更新版本。请联系管理员准备 Python。' >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  printf '%s\n' '当前 Python 版本低于 3.11，请使用 Python 3.11 或更新版本。' >&2
  exit 1
fi
if ! python3 -m venv .venv; then
  printf '%s\n' '无法创建 Python 运行环境。请联系管理员准备 python3-venv／ensurepip，再重新运行此脚本。' >&2
  exit 1
fi
if [ -d wheelhouse ]; then
  .venv/bin/python -m pip install --no-index --find-links wheelhouse -r requirements.txt
else
  .venv/bin/python -m pip install -r requirements.txt
fi
printf '%s\n' '环境准备完成。运行 sh start.sh 启动检测页面。'
