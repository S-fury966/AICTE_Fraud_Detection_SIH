#!/bin/sh
# Seed the /app/data volume on first start.
#
# Committed data (issuer registry, mock AICTE records, website snapshots) is
# baked into the image at /app/data_seed. On a fresh volume mounted at
# /app/data, copy any missing files across so the detectors have what they
# expect. Existing files are never overwritten.
set -e

SEED=/app/data_seed
TARGET=/app/data

if [ -d "$SEED" ]; then
    mkdir -p "$TARGET"
    # -n: never overwrite existing files on the volume.
    cp -rn "$SEED/." "$TARGET/" 2>/dev/null || true
fi

exec "$@"
