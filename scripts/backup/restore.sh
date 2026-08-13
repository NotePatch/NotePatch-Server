#!/bin/sh
set -eu
snapshot="${1:-}"
target="${2:-}"
if [ -z "$snapshot" ] || [ -z "$target" ]; then echo "usage: restore.sh SNAPSHOT EXPLICIT_EMPTY_TARGET" >&2; exit 2; fi
if [ "$target" = "/" ] || [ -e "$target" ]; then echo "target must not already exist" >&2; exit 2; fi
mkdir -p "$target"
restic restore "$snapshot" --target "$target"
