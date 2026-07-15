#!/bin/sh
# ThinCart entrypoint — starts as ROOT on purpose, for exactly one job:
# Fly mounts the /data volume owned by root, and anything `fly sftp`/`fly ssh`
# places there is root-owned too. Without the boot-time chown the app (uid
# 10001) cannot create or reopen its database — first boot crash-loops, and a
# post-swap restart reopens a root-owned file read-only. After the chown we
# drop privileges permanently and exec uvicorn.
set -eu

if [ -d /data ]; then
    chown -R thincart:thincart /data
fi

# --clear-groups is load-bearing: setpriv refuses --regid without a
# group-handling flag (exits 1 → the container would crash-loop).
exec setpriv --reuid thincart --regid thincart --clear-groups \
    uvicorn app:app --host 0.0.0.0 --port 8123
