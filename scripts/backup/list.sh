#!/bin/sh
set -eu
restic snapshots --tag notepatch-daily
