#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export GDK_BACKEND="${GDK_BACKEND:-x11}"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m ds_pet.app "$@"
