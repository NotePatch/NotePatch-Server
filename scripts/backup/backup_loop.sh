#!/bin/sh
set -eu
while true; do
  now="$(date +%s)"
  next="$(date -d 'tomorrow 02:30' +%s)"
  sleep "$((next-now))"
  /opt/notepatch/scripts/backup/backup_once.sh || true
done
