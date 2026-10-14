#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  sh setup.sh
fi
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  exec .venv/bin/python -m epics_field_client --no-browser "$@"
fi
exec .venv/bin/python -m epics_field_client "$@"
