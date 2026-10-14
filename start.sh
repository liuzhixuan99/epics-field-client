#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  printf '%s\n' '尚未准备运行环境。请先运行：sh setup.sh' >&2
  exit 1
fi
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  exec .venv/bin/python -m epics_field_client --no-browser "$@"
fi
exec .venv/bin/python -m epics_field_client "$@"
